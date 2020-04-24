#!/usr/bin/env python3

# Copyright 2020, The Android Open Source Project
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use, copy,
# modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
"""Command-line tool for AFTL support for Android Verified Boot images."""

import argparse
import base64
import hashlib
import json
import multiprocessing
import os
import queue
import struct
import subprocess
import sys
import tempfile
import time

# This is to work around temporarily with the issue that python3 does not permit
# relative imports anymore going forward. This adds the proto directory relative
# to the location of aftltool to the sys.path.
# TODO(b/154068467): Implement proper importing of generated *_pb2 modules.
EXEC_PATH = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(EXEC_PATH, 'proto'))

# pylint: disable=wrong-import-position,import-error
import avbtool
import aftl_pb2
import api_pb2
from crypto.sigpb import sigpb_pb2
# pylint: enable=wrong-import-position,import-error


class AftlError(Exception):
  """Application-specific errors.

  These errors represent issues for which a stack-trace should not be
  presented.

  Attributes:
    message: Error message.
  """

  def __init__(self, message):
    Exception.__init__(self, message)


def rsa_key_read_pem_bytes(key_path):
  """Reads the bytes out of the passed in PEM file.

  Arguments:
    key_path: A string containing the path to the PEM file.

  Returns:
    A bytearray containing the DER encoded bytes in the PEM file.

  Raises:
    AftlError: If openssl cannot decode the PEM file.
  """
  # Use openssl to decode the PEM file.
  args = ['openssl', 'rsa', '-in', key_path, '-pubout', '-outform', 'DER']
  p = subprocess.Popen(args,
                       stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
  (pout, perr) = p.communicate()
  retcode = p.wait()
  if retcode != 0:
    raise AftlError('Error decoding: {}'.format(perr))
  return pout


def check_signature(log_root, log_root_sig,
                    transparency_log_pub_key):
  """Validates the signature provided by the transparency log.

  Arguments:
    log_root: The transparency log_root data structure.
    log_root_sig: The signature of the transparency log_root data structure.
    transparency_log_pub_key: The file path to the transparency log public key.

  Returns:
    True if the signature check passes, otherwise False.
  """

  logsig_tmp = tempfile.NamedTemporaryFile()
  logsig_tmp.write(log_root_sig)
  logsig_tmp.flush()
  logroot_tmp = tempfile.NamedTemporaryFile()
  logroot_tmp.write(log_root)
  logroot_tmp.flush()

  p = subprocess.Popen(['openssl', 'dgst', '-sha256', '-verify',
                        transparency_log_pub_key,
                        '-signature', logsig_tmp.name, logroot_tmp.name],
                       stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

  p.communicate()
  retcode = p.wait()
  if not retcode:
    return True
  return False


# AFTL Merkle Tree Functionality
def rfc6962_hash_leaf(leaf):
  """RFC6962 hashing function for hashing leaves of a Merkle tree.

  Arguments:
    leaf: A bytearray containing the Merkle tree leaf to be hashed.

  Returns:
    A bytearray containing the RFC6962 SHA256 hash of the leaf.
  """
  hasher = hashlib.sha256()
  # RFC6962 states a '0' byte should be prepended to the data.
  # This is done in conjunction with the '1' byte for non-leaf
  # nodes for 2nd preimage attack resistance.
  hasher.update(b'\x00')
  hasher.update(leaf)
  return hasher.digest()


def rfc6962_hash_children(l, r):
  """Calculates the inner Merkle tree node hash of child nodes l and r.

  Arguments:
    l: A bytearray containing the left child node to be hashed.
    r: A bytearray containing the right child node to be hashed.

  Returns:
    A bytearray containing the RFC6962 SHA256 hash of 1|l|r.
  """
  hasher = hashlib.sha256()
  # RFC6962 states a '1' byte should be prepended to the concatenated data.
  # This is done in conjunction with the '0' byte for leaf
  # nodes for 2nd preimage attack resistance.
  hasher.update(b'\x01')
  hasher.update(l)
  hasher.update(r)
  return hasher.digest()


def chain_border_right(seed, proof):
  """Computes a subtree hash along the left-side tree border.

  Arguments:
    seed: A bytearray containing the starting hash.
    proof: A list of bytearrays representing the hashes in the inclusion proof.

  Returns:
    A bytearray containing the left-side subtree hash.
  """
  for h in proof:
    seed = rfc6962_hash_children(h, seed)
  return seed


def chain_inner(seed, proof, leaf_index):
  """Computes a subtree hash on or below the tree's right border.

  Arguments:
    seed: A bytearray containing the starting hash.
    proof: A list of bytearrays representing the hashes in the inclusion proof.
    leaf_index: The current leaf index.

  Returns:
    A bytearray containing the subtree hash.
  """
  for i, h in enumerate(proof):
    if leaf_index >> i & 1 == 0:
      seed = rfc6962_hash_children(seed, h)
    else:
      seed = rfc6962_hash_children(h, seed)
  return seed


def root_from_icp(leaf_index, tree_size, proof, leaf_hash):
  """Calculates the expected Merkle tree root hash.

  Arguments:
    leaf_index: The current leaf index.
    tree_size: The number of nodes in the Merkle tree.
    proof: A list of bytearrays containing the inclusion proof.
    leaf_hash: A bytearray containing the initial leaf hash.

  Returns:
    A bytearray containing the calculated Merkle tree root hash.

  Raises:
    AftlError: If invalid parameters are passed in.
  """
  if leaf_index < 0:
    raise AftlError('Invalid leaf_index value: {}'.format(leaf_index))
  if tree_size < 0:
    raise AftlError('Invalid tree_size value: {}'.format(tree_size))
  if leaf_index >= tree_size:
    err_str = 'leaf_index cannot be equal or larger than tree_size: {}, {}'
    raise AftlError(err_str.format(leaf_index, tree_size))
  if proof is None:
    raise AftlError('Inclusion proof not provided.')
  if leaf_hash is None:
    raise AftlError('No leaf hash provided.')
  # Calculate the point to split the proof into two parts.
  # The split is where the paths to leaves diverge.
  inner = (leaf_index ^ (tree_size - 1)).bit_length()
  result = chain_inner(leaf_hash, proof[:inner], leaf_index)
  result = chain_border_right(result, proof[inner:])
  return result


class AftlImageHeader(object):
  """A class for representing the AFTL image header.

  Attributes:
    magic: Magic for identifying the AftlImage.
    required_icp_version_major: The major version of AVB that wrote the entry.
    required_icp_version_minor: The minor version of AVB that wrote the entry.
    aftl_image_size: Total size of the AftlImage.
    icp_count: Number of inclusion proofs represented in this structure.
  """

  SIZE = 18  # The size of the structure, in bytes
  MAGIC = b'AFTL'
  FORMAT_STRING = ('!4s2L'  # magic, major & minor version.
                   'L'      # AFTL image size.
                   'H')     # number of inclusion proof entries.

  def __init__(self, data=None):
    """Initializes a new AftlImageHeader object.

    Arguments:
      data: If not None, must be a bytearray of size |SIZE|.

    Raises:
      AftlError: If invalid structure for AftlImageHeader.
    """
    assert struct.calcsize(self.FORMAT_STRING) == self.SIZE

    if data:
      (self.magic, self.required_icp_version_major,
       self.required_icp_version_minor, self.aftl_image_size,
       self.icp_count) = struct.unpack(self.FORMAT_STRING, data)
    else:
      self.magic = self.MAGIC
      self.required_icp_version_major = avbtool.AVB_VERSION_MAJOR
      self.required_icp_version_minor = avbtool.AVB_VERSION_MINOR
      self.aftl_image_size = self.SIZE
      self.icp_count = 0
    if not self.is_valid():
      raise AftlError('Invalid structure for AftlImageHeader.')

  def encode(self):
    """Serializes the AftlImageHeader |SIZE| to bytes.

    Returns:
      The encoded AftlImageHeader as bytes.

    Raises:
      AftlError: If invalid structure for AftlImageHeader.
    """
    if not self.is_valid():
      raise AftlError('Invalid structure for AftlImageHeader')
    return struct.pack(self.FORMAT_STRING, self.magic,
                       self.required_icp_version_major,
                       self.required_icp_version_minor,
                       self.aftl_image_size,
                       self.icp_count)

  def is_valid(self):
    """Ensures that values in the AftlImageHeader are sane.

    Returns:
      True if the values in the AftlImageHeader are sane, False otherwise.
    """
    if self.magic != AftlImageHeader.MAGIC:
      sys.stderr.write(
          'AftlImageHeader: magic value mismatch: {}\n'
          .format(repr(self.magic)))
      return False

    if self.required_icp_version_major > avbtool.AVB_VERSION_MAJOR:
      sys.stderr.write('AftlImageHeader: major version mismatch: {}\n'.format(
          self.required_icp_version_major))
      return False

    if self.required_icp_version_minor > avbtool.AVB_VERSION_MINOR:
      sys.stderr.write('AftlImageHeader: minor version mismatch: {}\n'.format(
          self.required_icp_version_minor))
      return False

    if self.aftl_image_size < self.SIZE:
      sys.stderr.write('AftlImageHeader: Invalid AFTL image size: {}\n'.format(
          self.aftl_image_size))
      return False

    if self.icp_count < 0 or self.icp_count > 65535:
      sys.stderr.write(
          'AftlImageHeader: ICP entry count out of range: {}\n'.format(
              self.icp_count))
      return False
    return True

  def print_desc(self, o):
    """Print the AftlImageHeader.

    Arguments:
      o: The object to write the output to.
    """
    o.write('  AFTL image header:\n')
    i = ' ' * 4
    fmt = '{}{:25}{}\n'
    o.write(fmt.format(i, 'Major version:', self.required_icp_version_major))
    o.write(fmt.format(i, 'Minor version:', self.required_icp_version_minor))
    o.write(fmt.format(i, 'Image size:', self.aftl_image_size))
    o.write(fmt.format(i, 'ICP entries count:', self.icp_count))


class AftlIcpEntry(object):
  """A class for the transparency log inclusion proof entries.

  The data that represents each of the components of the ICP entry are stored
  immediately following the ICP entry header. The format is log_url,
  SignedLogRoot, and inclusion proof hashes.

  Attributes:
    log_url_size: Length of the string representing the transparency log URL.
    leaf_index: Leaf index in the transparency log representing this entry.
    log_root_descriptor_size: Size of the transparency log's SignedLogRoot.
    fw_info_leaf_size: Size of the FirmwareInfo leaf passed to the log.
    log_root_sig_size: Size in bytes of the log_root_signature
    proof_hash_count: Number of hashes comprising the inclusion proof.
    inc_proof_size: The total size of the inclusion proof, in bytes.
    log_url: The URL for the transparency log that generated this inclusion
        proof.
    log_root_descriptor: The data comprising the signed tree head structure.
    fw_info_leaf: The data comprising the FirmwareInfo leaf.
    log_root_signature: The data comprising the log root signature.
    proofs: The hashes comprising the inclusion proof.

  """
  SIZE = 27  # The size of the structure, in bytes
  FORMAT_STRING = ('!L'   # transparency log server url size
                   'Q'    # leaf index
                   'L'    # log root descriptor size
                   'L'    # firmware info leaf size
                   'H'    # log root signature size
                   'B'    # number of hashes in the inclusion proof
                   'L')   # size of the inclusion proof in bytes
  # These are used to capture the log_url, log_root_descriptor,
  # fw_info leaf, log root signature, and the proofs elements for the
  # encode function.

  def __init__(self, data=None):
    """Initializes a new ICP entry object.

    Arguments:
      data: If not None, must be a bytearray of size >= |SIZE|.

    Raises:
      AftlError: If data does not represent a well-formed AftlIcpEntry.
    """
    # Assert the header structure is of a sane size.
    assert struct.calcsize(self.FORMAT_STRING) == self.SIZE

    if data:
      # Deserialize the header from the data.
      (self._log_url_size_expected,
       self.leaf_index,
       self._log_root_descriptor_size_expected,
       self._fw_info_leaf_size_expected,
       self._log_root_sig_size_expected,
       self._proof_hash_count_expected,
       self._inc_proof_size_expected) = struct.unpack(self.FORMAT_STRING,
                                                      data[0:self.SIZE])

      # Deserialize ICP entry components from the data.
      expected_format_string = '{}s{}s{}s{}s{}s'.format(
          self._log_url_size_expected,
          self._log_root_descriptor_size_expected,
          self._fw_info_leaf_size_expected,
          self._log_root_sig_size_expected,
          self._inc_proof_size_expected)

      (log_url, log_root_descriptor_bytes, fw_info_leaf_bytes,
       self.log_root_signature, proof_bytes) = struct.unpack(
           expected_format_string, data[self.SIZE:self.get_expected_size()])

      self.log_url = log_url.decode('ascii')
      self.log_root_descriptor = TrillianLogRootDescriptor(
          log_root_descriptor_bytes)
      self.fw_info_leaf = FirmwareInfoLeaf(fw_info_leaf_bytes)

      self.proofs = []
      if self._proof_hash_count_expected > 0:
        proof_idx = 0
        hash_size = (self._inc_proof_size_expected
                     // self._proof_hash_count_expected)
        for _ in range(self._proof_hash_count_expected):
          proof = proof_bytes[proof_idx:(proof_idx+hash_size)]
          self.proofs.append(proof)
          proof_idx += hash_size
    else:
      self.leaf_index = 0
      self.log_url = ''
      self.log_root_descriptor = TrillianLogRootDescriptor()
      self.fw_info_leaf = FirmwareInfoLeaf()
      self.log_root_signature = b''
      self.proofs = []
    if not self.is_valid():
      raise AftlError('Invalid structure for AftlIcpEntry')

  @property
  def log_url_size(self):
    """Gets the size of the log_url attribute."""
    if hasattr(self, 'log_url'):
      return len(self.log_url)
    return self._log_url_size_expected

  @property
  def log_root_descriptor_size(self):
    """Gets the size of the log_root_descriptor attribute."""
    if hasattr(self, 'log_root_descriptor'):
      return self.log_root_descriptor.get_expected_size()
    return self._log_root_descriptor_size_expected

  @property
  def fw_info_leaf_size(self):
    """Gets the size of the fw_info_leaf attribute."""
    if hasattr(self, 'fw_info_leaf'):
      return self.fw_info_leaf.get_expected_size()
    return self._fw_info_leaf_size_expected

  @property
  def log_root_sig_size(self):
    """Gets the size of the log_root signature."""
    if hasattr(self, 'log_root_signature'):
      return len(self.log_root_signature)
    return self._log_root_sig_size_expected

  @property
  def proof_hash_count(self):
    """Gets the number of proof hashes."""
    if hasattr(self, 'proofs'):
      return len(self.proofs)
    return self._proof_hash_count_expected

  @property
  def inc_proof_size(self):
    """Gets the total size of the proof hashes in bytes."""
    if hasattr(self, 'proofs'):
      result = 0
      for proof in self.proofs:
        result += len(proof)
      return result
    return self._inc_proof_size_expected

  def verify_icp(self, transparency_log_pub_key):
    """Verifies the contained inclusion proof given the public log key.

    Arguments:
      transparency_log_pub_key: The path to the trusted public key for the log.

    Returns:
      True if the calculated signature matches AftlIcpEntry's. False otherwise.
    """
    if not transparency_log_pub_key:
      return False

    leaf_hash = rfc6962_hash_leaf(self.fw_info_leaf.encode())
    calc_root = root_from_icp(self.leaf_index,
                              self.log_root_descriptor.tree_size,
                              self.proofs,
                              leaf_hash)
    if ((calc_root == self.log_root_descriptor.root_hash) and
        check_signature(
            self.log_root_descriptor.encode(),
            self.log_root_signature,
            transparency_log_pub_key)):
      return True
    return False

  def verify_vbmeta_image(self, vbmeta_image, transparency_log_pub_key):
    """Verify the inclusion proof for the given VBMeta image.

    Arguments:
      vbmeta_image: A bytearray with the VBMeta image.
      transparency_log_pub_key: File path to the PEM file containing the trusted
        transparency log public key.

    Returns:
      True if the inclusion proof validates and the vbmeta hash of the given
      VBMeta image matches the one in the fw_info_leaf; otherwise False.
    """
    if not vbmeta_image:
      return False

    # Calculate the hash of the vbmeta image.
    vbmeta_hash = hashlib.sha256(vbmeta_image).digest()

    # Validates the inclusion proof and then compare the calculated vbmeta_hash
    # against the one in the inclusion proof.
    return (self.verify_icp(transparency_log_pub_key)
            and self.fw_info_leaf.vbmeta_hash == vbmeta_hash)

  def encode(self):
    """Serializes the header |SIZE| and data to a bytearray().

    Returns:
      A bytearray() with the encoded header.

    Raises:
      AftlError: If invalid entry structure.
    """
    proof_bytes = bytearray()
    if not self.is_valid():
      raise AftlError('Invalid AftlIcpEntry structure')

    expected_format_string = '{}{}s{}s{}s{}s{}s'.format(
        self.FORMAT_STRING,
        self.log_url_size,
        self.log_root_descriptor_size,
        self.fw_info_leaf_size,
        self.log_root_sig_size,
        self.inc_proof_size)

    for proof in self.proofs:
      proof_bytes.extend(proof)

    return struct.pack(expected_format_string,
                       self.log_url_size, self.leaf_index,
                       self.log_root_descriptor_size, self.fw_info_leaf_size,
                       self.log_root_sig_size, self.proof_hash_count,
                       self.inc_proof_size, self.log_url.encode('ascii'),
                       self.log_root_descriptor.encode(),
                       self.fw_info_leaf.encode(),
                       self.log_root_signature,
                       proof_bytes)

  def translate_response(self, log_url, afi_response):
    """Translates an AddFirmwareInfoResponse object to an AftlIcpEntry.

    Arguments:
      log_url: String representing the transparency log URL.
      afi_response: The AddFirmwareResponse object to translate.
    """
    self.log_url = log_url

    # Deserializes from AddFirmwareInfoResponse.
    self.leaf_index = afi_response.fw_info_proof.proof.leaf_index
    self.log_root_descriptor = TrillianLogRootDescriptor(
        afi_response.fw_info_proof.sth.log_root)
    self.fw_info_leaf = FirmwareInfoLeaf(afi_response.fw_info_leaf)
    self.log_root_signature = afi_response.fw_info_proof.sth.log_root_signature
    self.proofs = afi_response.fw_info_proof.proof.hashes

  def get_expected_size(self):
    """Gets the expected size of the full entry out of the header.

    Returns:
      The expected size of the AftlIcpEntry from the header.
    """
    return (self.SIZE + self.log_url_size + self.log_root_descriptor_size +
            self.fw_info_leaf_size + self.log_root_sig_size +
            self.inc_proof_size)

  def is_valid(self):
    """Ensures that values in an AftlIcpEntry structure are sane.

    Returns:
      True if the values in the AftlIcpEntry are sane, False otherwise.
    """
    if self.leaf_index < 0:
      sys.stderr.write('ICP entry: leaf index out of range: '
                       '{}.\n'.format(self.leaf_index))
      return False

    if (not self.log_root_descriptor or
        not isinstance(self.log_root_descriptor, TrillianLogRootDescriptor) or
        not self.log_root_descriptor.is_valid()):
      sys.stderr.write('ICP entry: invalid TrillianLogRootDescriptor.\n')
      return False

    if (not self.fw_info_leaf or
        not isinstance(self.fw_info_leaf, FirmwareInfoLeaf)):
      sys.stderr.write('ICP entry: invalid FirmwareInfo.\n')
      return False
    return True

  def print_desc(self, o):
    """Print the ICP entry.

    Arguments:
      o: The object to write the output to.
    """
    i = ' ' * 4
    fmt = '{}{:25}{}\n'
    o.write(fmt.format(i, 'Transparency Log:', self.log_url))
    o.write(fmt.format(i, 'Leaf index:', self.leaf_index))
    o.write('    ICP hashes:              ')
    for i, proof_hash in enumerate(self.proofs):
      if i != 0:
        o.write(' ' * 29)
      o.write('{}\n'.format(proof_hash.hex()))
    self.log_root_descriptor.print_desc(o)
    self.fw_info_leaf.print_desc(o)


class TrillianLogRootDescriptor(object):
  """A class representing the Trillian log_root descriptor.

  Taken from Trillian definitions:
  https://github.com/google/trillian/blob/master/trillian.proto#L255

  Attributes:
    version: The version number of the descriptor. Currently only version=1 is
        supported.
    tree_size: The size of the tree.
    root_hash_size: The size of the root hash in bytes. Valid values are between
        0 and 128.
    root_hash: The root hash as bytearray().
    timestamp: The timestamp in nanoseconds.
    revision: The revision number as long.
    metadata_size: The size of the metadata in bytes. Valid values are between
        0 and 65535.
    metadata: The metadata as bytearray().
  """
  FORMAT_STRING_PART_1 = ('!H'  # version
                          'Q'   # tree_size
                          'B'   # root_hash_size
                         )

  FORMAT_STRING_PART_2 = ('!Q'  # timestamp
                          'Q'   # revision
                          'H'   # metadata_size
                         )

  def __init__(self, data=None):
    """Initializes a new TrillianLogRoot descriptor."""
    if data:
      # Parses first part of the log_root descriptor.
      data_length = struct.calcsize(self.FORMAT_STRING_PART_1)
      (self.version, self.tree_size, self.root_hash_size) = struct.unpack(
          self.FORMAT_STRING_PART_1, data[0:data_length])
      data = data[data_length:]

      # Parses the root_hash bytes if the size indicates existance.
      if self.root_hash_size > 0:
        self.root_hash = data[0:self.root_hash_size]
        data = data[self.root_hash_size:]
      else:
        self.root_hash = b''

      # Parses second part of the log_root descriptor.
      data_length = struct.calcsize(self.FORMAT_STRING_PART_2)
      (self.timestamp, self.revision, self.metadata_size) = struct.unpack(
          self.FORMAT_STRING_PART_2, data[0:data_length])
      data = data[data_length:]

      # Parses the metadata if the size indicates existance.
      if self.metadata_size > 0:
        self.metadata = data[0:self.metadata_size]
      else:
        self.metadata = b''
    else:
      self.version = 1
      self.tree_size = 0
      self.root_hash_size = 0
      self.root_hash = b''
      self.timestamp = 0
      self.revision = 0
      self.metadata_size = 0
      self.metadata = b''

    if not self.is_valid():
      raise AftlError('Invalid structure for TrillianLogRootDescriptor.')

  def get_expected_size(self):
    """Calculates the expected size of the TrillianLogRootDescriptor.

    Returns:
      The expected size of the TrillianLogRootDescriptor.
    """
    return (struct.calcsize(self.FORMAT_STRING_PART_1) + self.root_hash_size +
            struct.calcsize(self.FORMAT_STRING_PART_2) + self.metadata_size)

  def encode(self):
    """Serializes the TrillianLogDescriptor to a bytearray().

    Returns:
      A bytearray() with the encoded header.

    Raises:
      AftlError: If invalid entry structure.
    """
    if not self.is_valid():
      raise AftlError('Invalid structure for TrillianLogRootDescriptor.')

    expected_format_string = '{}{}s{}{}s'.format(
        self.FORMAT_STRING_PART_1,
        self.root_hash_size,
        self.FORMAT_STRING_PART_2[1:],
        self.metadata_size)

    return struct.pack(expected_format_string,
                       self.version, self.tree_size, self.root_hash_size,
                       self.root_hash, self.timestamp, self.revision,
                       self.metadata_size, self.metadata)

  def is_valid(self):
    """Ensures that values in the descritor are sane.

    Returns:
      True if the values are sane; otherwise False.
    """
    cls = self.__class__.__name__
    if self.version != 1:
      sys.stderr.write('{}: Bad version value {}.\n'.format(cls, self.version))
      return False
    if self.tree_size < 0:
      sys.stderr.write('{}: Bad tree_size value {}.\n'.format(cls,
                                                              self.tree_size))
      return False
    if self.root_hash_size < 0 or self.root_hash_size > 128:
      sys.stderr.write('{}: Bad root_hash_size value {}.\n'.format(
          cls, self.root_hash_size))
      return False
    if len(self.root_hash) != self.root_hash_size:
      sys.stderr.write('{}: root_hash_size {} does not match with length of '
                       'root_hash {}.\n'.format(cls, self.root_hash_size,
                                                len(self.root_hash)))
      return False
    if self.timestamp < 0:
      sys.stderr.write('{}: Bad timestamp value {}.\n'.format(cls,
                                                              self.timestamp))
      return False
    if self.revision < 0:
      sys.stderr.write('{}: Bad revision value {}.\n'.format(cls,
                                                             self.revision))
      return False
    if self.metadata_size < 0 or self.metadata_size > 65535:
      sys.stderr.write('{}: Bad metadatasize value {}.\n'.format(
          cls, self.metadata_size))
      return False
    if len(self.metadata) != self.metadata_size:
      sys.stderr.write('{}: metadata_size {} does not match with length of'
                       'metadata {}.\n'.format(cls, self.metadata_size,
                                               len(self.metadata)))
      return False
    return True

  def print_desc(self, o):
    """Print the TrillianLogRootDescriptor.

    Arguments:
      o: The object to write the output to.
    """
    o.write('    Log Root Descriptor:\n')
    i = ' ' * 6
    fmt = '{}{:23}{}\n'
    o.write(fmt.format(i, 'Version:', self.version))
    o.write(fmt.format(i, 'Tree size:', self.tree_size))
    o.write(fmt.format(i, 'Root hash size:', self.root_hash_size))
    if self.root_hash_size > 0:
      o.write(fmt.format(i, 'Root hash:', self.root_hash.hex()))
      o.write(fmt.format(i, 'Timestamp (ns):', self.timestamp))
    o.write(fmt.format(i, 'Revision:', self.revision))
    o.write(fmt.format(i, 'Metadata size:', self.metadata_size))
    if self.metadata_size > 0:
      o.write(fmt.format(i, 'Metadata:', self.metadata.hex()))


class FirmwareInfoLeaf(object):
  """A class representing the FirmwareInfo leaf.

  AFTL returns the fw_info_leaf as a JSON blob and this class is able to
  parse the blog and provide necessary attributes needed for validation.

  Attributes:
    vbmeta_hash: This is the SHA256 hash of vbmeta.
    version_incremental: Subcomponent of the build fingerprint as defined at
      https://source.android.com/compatibility/android-cdd#3_2_2_build_parameters.
      For example, a Pixel device with the following build fingerprint
      google/crosshatch/crosshatch:9/PQ3A.190605.003/5524043:user/release-keys,
      would have 5524043 for the version incremental.
    platform_key: Public key of the platform. This is the same key used to sign
      the vbmeta.
    manufacturer_key_hash:  SHA256 of the manufacturer public key (DER-encoded,
      x509 subjectPublicKeyInfo format). The public key MUST already be in the
      list of root keys known and trusted by the AFTL.
    description: Free form description field. It can be used to annotate this
      message with further context on the build (e.g., carrier specific build).
  """

  def __init__(self, data=None):
    """Initializes a new FirmwareInfoLeaf descriptor."""
    if data:
      # We have to preserve the original fw_info_leaf bytes in order to preserve
      # hash equivalence with what is stored in the Trillian log and matches up
      # with the proofs.
      self._fw_info_leaf_bytes = data

      # Deserialize the JSON blob and keep only the FirmwareInfo parts.
      try:
        fw_info_leaf = json.loads(self._fw_info_leaf_bytes)
        self._fw_info_leaf_dict = (
            fw_info_leaf['Value']['FwInfo']['info']['info'])
      except (ValueError, KeyError) as e:
        raise AftlError('Invalid structure for FirmwareInfoLeaf: {}'.format(e))
    else:
      self._fw_info_leaf_bytes = b''
      self._fw_info_leaf_dict = dict()

    if not self.is_valid():
      raise AftlError('Invalid structure for FirmwareInfoLeaf.')

  @property
  def vbmeta_hash(self):
    """Gets the vbmeta_hash attribute."""
    return self._lookup_base64_attribute('vbmeta_hash')

  @property
  def version_incremental(self):
    """Gets the version_incremental attribute."""
    return self._fw_info_leaf_dict.get('version_incremental')

  @property
  def platform_key(self):
    """Gets the platform key attribute."""
    return self._lookup_base64_attribute('platform_key')

  @property
  def manufacturer_key_hash(self):
    """Gets the manufacturer_key_hash attribute."""
    return self._lookup_base64_attribute('manufacturer_key_hash')

  @property
  def description(self):
    """Gets the description attribute."""
    return self._fw_info_leaf_dict.get('description')

  def _lookup_base64_attribute(self, key):
    """Looks up an attribute that is Base64 encoded and decodes it.

    Arguments:
      key: The name of the attribute to look up.

    Returns:
      The attribute value or None if not defined.
    """
    result = self._fw_info_leaf_dict.get(key)
    if result:
      result = base64.b64decode(result)
    return result

  def get_expected_size(self):
    """Gets the expected size of the JSON-serialized FirmwareInfoLeaf.

    Returns:
      The expected size of the FirmwareInfo leaf in byte or 0 if not initalized.
    """
    if not self._fw_info_leaf_bytes:
      return 0
    return len(self._fw_info_leaf_bytes)

  def encode(self):
    """Serializes the FirmwareInfoLeaf.

    Returns:
      A bytearray() with the JSON-serialized FirmwareInfoLeaf.
    """
    return self._fw_info_leaf_bytes

  def is_valid(self):
    """Ensures that values in the descritor are sane.

    Returns:
      True if the values are sane; otherwise False.
    """
    # Checks that the decode fw_info_leaf at max contains values defined in the
    # FirmwareInfo proto buf.
    expected_fields = set(aftl_pb2.FirmwareInfo()
                          .DESCRIPTOR.fields_by_name.keys())
    actual_fields = set(self._fw_info_leaf_dict.keys())
    if actual_fields.issubset(expected_fields):
      return True
    return False

  def print_desc(self, o):
    """Print the FirmwareInfoLeaf.

    Arguments:
      o: The object to write the output to.
    """
    o.write('    Firmware Info Leaf:\n')
    # The order of the fields is based on the definition in
    # proto.aftl_pb2.FirmwareInfo.
    i = ' ' * 6
    fmt = '{}{:23}{}\n'
    if self.vbmeta_hash:
      o.write(fmt.format(i, 'VBMeta hash:', self.vbmeta_hash.hex()))
    if self.version_incremental:
      o.write(fmt.format(i, 'Version incremental:', self.version_incremental))
    if self.platform_key:
      o.write(fmt.format(i, 'Platform key:', self.platform_key))
    if self.manufacturer_key_hash:
      o.write(fmt.format(i, 'Manufacturer key hash:',
                         self.manufacturer_key_hash.hex()))
    if self.description:
      o.write(fmt.format(i, 'Description:', self.description))


class AftlImage(object):
  """A class for the AFTL image, which contains the transparency log ICPs.

  This encapsulates an AFTL ICP section with all information required to
  validate an inclusion proof.

  Attributes:
    image_header: A header for the section.
    icp_entries: A list of AftlIcpEntry objects representing the inclusion
        proofs.
  """

  def __init__(self, data=None):
    """Initializes a new AftlImage section.

    Arguments:
      data: If not None, must be a bytearray representing an AftlImage.

    Raises:
      AftlError: If the data does not represent a well-formed AftlImage.
    """
    if data:
      image_header_bytes = data[0:AftlImageHeader.SIZE]
      self.image_header = AftlImageHeader(image_header_bytes)
      if not self.image_header.is_valid():
        raise AftlError('Invalid AftlImageHeader.')
      icp_count = self.image_header.icp_count

      # Jump past the header for entry deserialization.
      icp_index = AftlImageHeader.SIZE
      # Validate each entry.
      self.icp_entries = []
      # add_icp_entry() updates entries and header, so set header count to
      # compensate.
      self.image_header.icp_count = 0
      for i in range(icp_count):
        # Get the entry header from the AftlImage.
        cur_icp_entry = AftlIcpEntry(data[icp_index:])
        cur_icp_entry_size = cur_icp_entry.get_expected_size()
        # Now validate the entry structure.
        if not cur_icp_entry.is_valid():
          raise AftlError('Validation of ICP entry {} failed.'.format(i))
        self.add_icp_entry(cur_icp_entry)
        icp_index += cur_icp_entry_size
    else:
      self.image_header = AftlImageHeader()
      self.icp_entries = []
    if not self.is_valid():
      raise AftlError('Invalid AftlImage.')

  def add_icp_entry(self, icp_entry):
    """Adds a new AftlIcpEntry to the AftlImage, updating fields as needed.

    Arguments:
      icp_entry: An AftlIcpEntry structure.
    """
    self.icp_entries.append(icp_entry)
    self.image_header.icp_count += 1
    self.image_header.aftl_image_size += icp_entry.get_expected_size()

  def verify_vbmeta_image(self, vbmeta_image, transparency_log_pub_keys):
    """Verifies the contained inclusion proof given the public log key.

    Arguments:
      vbmeta_image: The vbmeta_image that should be verified against the
        inclusion proof.
      transparency_log_pub_keys: List of paths to PEM files containing trusted
        public keys that correspond with the transparency_logs.

    Returns:
      True if all the inclusion proofs in the AfltDescriptor validate, are
      signed by one of the give transparency log public keys; otherwise false.
    """
    if not transparency_log_pub_keys or not self.icp_entries:
      return False

    icp_verified = 0
    for icp_entry in self.icp_entries:
      verified = False
      for pub_key in transparency_log_pub_keys:
        if icp_entry.verify_vbmeta_image(vbmeta_image, pub_key):
          verified = True
          break
      if verified:
        icp_verified += 1
    return icp_verified == len(self.icp_entries)

  def encode(self):
    """Serialize the AftlImage to a bytearray().

    Returns:
      A bytearray() with the encoded AFTL image.

    Raises:
      AftlError: If invalid AFTL image structure.
    """
    # The header and entries are guaranteed to be valid when encode is called.
    # Check the entire structure as a whole.
    if not self.is_valid():
      raise AftlError('Invalid AftlImage structure.')

    aftl_image = bytearray()
    aftl_image.extend(self.image_header.encode())
    for icp_entry in self.icp_entries:
      aftl_image.extend(icp_entry.encode())
    return aftl_image

  def is_valid(self):
    """Ensures that values in the AftlImage are sane.

    Returns:
      True if the values in the AftlImage are sane, False otherwise.
    """
    if not self.image_header.is_valid():
      return False

    if self.image_header.icp_count != len(self.icp_entries):
      return False

    for icp_entry in self.icp_entries:
      if not icp_entry.is_valid():
        return False
    return True

  def print_desc(self, o):
    """Print the AFTL image.

    Arguments:
      o: The object to write the output to.
    """
    o.write('Android Firmware Transparency Image:\n')
    self.image_header.print_desc(o)
    for i, icp_entry in enumerate(self.icp_entries):
      o.write('  Entry #{}:\n'.format(i + 1))
      icp_entry.print_desc(o)


class AftlCommunication(object):
  """Class to abstract the communication layer with the transparency log."""

  def __init__(self, transparency_log_config, timeout):
    """Initializes the object.

    Arguments:
      transparency_log_config: A TransparencyLogConfig instance.
      timeout: Duration in seconds before requests to the AFTL times out. A
        value of 0 or None means there will be no timeout.
    """
    self.transparency_log_config = transparency_log_config
    if timeout:
      self.timeout = timeout
    else:
      self.timeout = None

  def add_firmware_info(self, request):
    """Calls the AddFirmwareInfo RPC on the AFTL server.

    Arguments:
      request: A AddFirmwareInfoRequest message.

    Returns:
      An AddFirmwareInfoReponse message.

    Raises:
      AftlError: If grpc or the proto modules cannot be loaded, if there is an
        error communicating with the log.
    """
    raise NotImplementedError(
        'AddFirmwareInfo() needs to be implemented by subclass.')


class AftlGrpcCommunication(AftlCommunication):
  """Class that implements GRPC communication to the AFTL server."""

  def add_firmware_info(self, request):
    """Calls the AddFirmwareInfo RPC on the AFTL server.

    Arguments:
      request: A AddFirmwareInfoRequest message.

    Returns:
      An AddFirmwareInfoReponse message.

    Raises:
      AftlError: If grpc or the proto modules cannot be loaded, if there is an
        error communicating with the log.
    """
    # Import grpc now to avoid global dependencies as it otherwise breakes
    # running unittest with atest.
    try:
      import grpc  # pylint: disable=import-outside-toplevel
      from proto import api_pb2_grpc # pylint: disable=import-outside-toplevel
    except ImportError as e:
      err_str = 'grpc can be installed with python pip install grpcio.\n'
      raise AftlError('Failed to import module: ({}).\n{}'.format(e, err_str))

    # Set up the gRPC channel with the transparency log.
    sys.stdout.write('Preparing to request inclusion proof from {}. This could '
                     'take ~30 seconds for the process to complete.\n'.format(
                         self.transparency_log_config.target))
    channel = grpc.insecure_channel(self.transparency_log_config.target)
    stub = api_pb2_grpc.AFTLogStub(channel)

    metadata = []
    if self.transparency_log_config.api_key:
      metadata.append(('x-api-key', self.transparency_log_config.api_key))

    # Attempt to transmit to the transparency log.
    sys.stdout.write('ICP is about to be requested from transparency log '
                     'with domain {}.\n'.format(
                         self.transparency_log_config.target))
    try:
      response = stub.AddFirmwareInfo(request, timeout=self.timeout,
                                      metadata=metadata)
    except grpc.RpcError as e:
      raise AftlError('Error: grpc failure ({})'.format(e))
    return response


class Aftl(avbtool.Avb):
  """Business logic for aftltool command-line tool."""

  def get_vbmeta_image(self, image_filename):
    """Gets the VBMeta struct bytes from image.

    Arguments:
      image_filename: Image file to get information from.

    Returns:
      A tuple with following elements:
        1. A bytearray with the vbmeta structure or None if the file does not
           contain a VBMeta structure.
        2. The VBMeta image footer.
    """
    # Reads and parses the vbmeta image.
    try:
      image = avbtool.ImageHandler(image_filename)
    except (IOError, ValueError) as e:
      sys.stderr.write('The image does not contain a valid VBMeta structure: '
                       '{}.\n'.format(e))
      return None, None

    try:
      (footer, header, _, _) = self._parse_image(image)
    except avbtool.AvbError as e:
      sys.stderr.write('The image cannot be parsed: {}.\n'.format(e))
      return None, None

    # Seeks for the start of the vbmeta image and calculates its size.
    offset = 0
    if footer:
      offset = footer.vbmeta_offset
    vbmeta_image_size = (offset + header.SIZE
                         + header.authentication_data_block_size
                         + header.auxiliary_data_block_size)

    # Reads the vbmeta image bytes.
    try:
      image.seek(offset)
    except RuntimeError as e:
      sys.stderr.write('Given vbmeta image offset is invalid: {}.\n'.format(e))
      return None, None
    return image.read(vbmeta_image_size), footer

  def get_aftl_image(self, image_filename):
    """Gets the AftlImage from image.

    Arguments:
      image_filename: Image file to get information from.

    Returns:
      An AftlImage or None if the file does not contain a AftlImage.
    """
    # Reads the vbmeta image bytes.
    vbmeta_image, _ = self.get_vbmeta_image(image_filename)
    if not vbmeta_image:
      return None

    try:
      image = avbtool.ImageHandler(image_filename)
    except ValueError as e:
      sys.stderr.write('The image does not contain a valid VBMeta structure: '
                       '{}.\n'.format(e))
      return None

    # Seeks for the start of the AftlImage.
    try:
      image.seek(len(vbmeta_image))
    except RuntimeError as e:
      sys.stderr.write('Given AftlImage image offset is invalid: {}.\n'
                       .format(e))
      return None

    # Parses the header for the AftlImage size.
    tmp_header_bytes = image.read(AftlImageHeader.SIZE)
    if not tmp_header_bytes or len(tmp_header_bytes) != AftlImageHeader.SIZE:
      sys.stderr.write('This image does not contain an AftlImage.\n')
      return None

    try:
      tmp_header = AftlImageHeader(tmp_header_bytes)
    except AftlError as e:
      sys.stderr.write('This image does not contain a valid AftlImage: '
                       '{}.\n'.format(e))
      return None

    # Resets to the beginning of the AftlImage.
    try:
      image.seek(len(vbmeta_image))
    except RuntimeError as e:
      sys.stderr.write('Given AftlImage image offset is invalid: {}.\n'
                       .format(e))
      return None

    # Parses the full AftlImage.
    aftl_image_bytes = image.read(tmp_header.aftl_image_size)
    aftl_image = None
    try:
      aftl_image = AftlImage(aftl_image_bytes)
    except AftlError as e:
      sys.stderr.write('The image does not contain a valid AftlImage: '
                       '{}.\n'.format(e))
    return aftl_image

  def info_image_icp(self, vbmeta_image_path, output):
    """Implements the 'info_image_icp' command.

    Arguments:
      vbmeta_image_path: Image file to get information from.
      output: Output file to write human-readable information to (file object).

    Returns:
      True if the given image has an AftlImage and could successfully
      be processed; otherwise False.
    """
    aftl_image = self.get_aftl_image(vbmeta_image_path)
    if not aftl_image:
      return False
    aftl_image.print_desc(output)
    return True

  def verify_image_icp(self, vbmeta_image_path, transparency_log_pub_keys,
                       output):
    """Implements the 'verify_image_icp' command.

    Arguments:
      vbmeta_image_path: Image file to get information from.
      transparency_log_pub_keys: List of paths to PEM files containing trusted
        public keys that correspond with the transparency_logs.
      output: Output file to write human-readable information to (file object).

    Returns:
      True if for the given image the inclusion proof validates; otherwise
      False.
    """
    vbmeta_image, _ = self.get_vbmeta_image(vbmeta_image_path)
    aftl_image = self.get_aftl_image(vbmeta_image_path)
    if not aftl_image or not vbmeta_image:
      return False
    verified = aftl_image.verify_vbmeta_image(vbmeta_image,
                                              transparency_log_pub_keys)
    if not verified:
      output.write('The inclusion proofs for the image do not validate.\n')
      return False
    output.write('The inclusion proofs for the image successfully validate.\n')
    return True

  def request_inclusion_proof(self, transparency_log_config, vbmeta_image,
                              version_inc, manufacturer_key_path,
                              signing_helper, signing_helper_with_files,
                              timeout, aftl_comms=None):
    """Packages and sends a request to the specified transparency log.

    Arguments:
      transparency_log_config: A TransparencyLogConfig instance.
      vbmeta_image: A bytearray with the VBMeta image.
      version_inc: Subcomponent of the build fingerprint.
      manufacturer_key_path: Path to key used to sign messages sent to the
        transparency log servers.
      signing_helper: Program which signs a hash and returns a signature.
      signing_helper_with_files: Same as signing_helper but uses files instead.
      timeout: Duration in seconds before requests to the transparency log
        timeout.
      aftl_comms: A subclass of the AftlCommunication class. The default is
        to use AftlGrpcCommunication.

    Returns:
      An AftlIcpEntry with the inclusion proof for the log entry.

    Raises:
      AftlError: If grpc or the proto modules cannot be loaded, if there is an
         error communicating with the log, if the manufacturer_key_path
         cannot be decoded, or if the log submission cannot be signed.
    """
    # Calculate the hash of the vbmeta image.
    vbmeta_hash = hashlib.sha256(vbmeta_image).digest()

    # Extract the key data from the PEM file if of size 4096.
    manufacturer_key = avbtool.RSAPublicKey(manufacturer_key_path)
    if manufacturer_key.num_bits != 4096:
      raise AftlError('Manufacturer keys not of size 4096: {}'.format(
          manufacturer_key.num_bits))
    manufacturer_key_data = rsa_key_read_pem_bytes(manufacturer_key_path)

    # Calculate the hash of the manufacturer key data.
    m_key_hash = hashlib.sha256(manufacturer_key_data).digest()

    # Create an AddFirmwareInfoRequest protobuf for transmission to AFTL.
    fw_info = aftl_pb2.FirmwareInfo(vbmeta_hash=vbmeta_hash,
                                    version_incremental=version_inc,
                                    manufacturer_key_hash=m_key_hash)
    signed_fw_info = b''
    # AFTL supports SHA256_RSA4096 for now, more will be available.
    algorithm_name = 'SHA256_RSA4096'
    try:
      rsa_key = avbtool.RSAPublicKey(manufacturer_key_path)
      signed_fw_info = rsa_key.sign(algorithm_name, fw_info.SerializeToString(),
                                    signing_helper, signing_helper_with_files)
    except avbtool.AvbError as e:
      raise AftlError('Failed to sign FirmwareInfo with '
                      '--manufacturer_key: {}'.format(e))
    fw_info_sig = sigpb_pb2.DigitallySigned(
        hash_algorithm='SHA256',
        signature_algorithm='RSA',
        signature=signed_fw_info)

    sfw_info = aftl_pb2.SignedFirmwareInfo(info=fw_info,
                                           info_signature=fw_info_sig)
    request = api_pb2.AddFirmwareInfoRequest(
        vbmeta=vbmeta_image, fw_info=sfw_info)

    # Submit signed FirmwareInfo to the server.
    if not aftl_comms:
      aftl_comms = AftlGrpcCommunication(transparency_log_config, timeout)
    response = aftl_comms.add_firmware_info(request)

    # Return an AftlIcpEntry representing this response.
    icp_entry = AftlIcpEntry()
    icp_entry.translate_response(transparency_log_config.target, response)
    return icp_entry

  def make_icp_from_vbmeta(self, vbmeta_image_path, output,
                           signing_helper, signing_helper_with_files,
                           version_incremental, transparency_log_configs,
                           manufacturer_key, padding_size, timeout):
    """Generates a vbmeta image with inclusion proof given a vbmeta image.

    The AftlImage contains the information required to validate an inclusion
    proof for a specific vbmeta image. It consists of a header (struct
    AftlImageHeader) and zero or more entry structures (struct AftlIcpEntry)
    that contain the vbmeta leaf hash, tree size, root hash, inclusion proof
    hashes, and the signature for the root hash.

    The vbmeta image, its hash, the version_incremental part of the build
    fingerprint, and the hash of the manufacturer key are sent to the
    transparency log, with the message signed by the manufacturer key.
    An inclusion proof is calculated and returned. This inclusion proof is
    then packaged in an AftlImage structure. The existing vbmeta data is
    copied to a new file, appended with the AftlImage data, and written to
    output. Validation of the inclusion proof does not require
    communication with the transparency log.

    Arguments:
      vbmeta_image_path: Path to a vbmeta image file.
      output: File to write the results to.
      signing_helper: Program which signs a hash and returns a signature.
      signing_helper_with_files: Same as signing_helper but uses files instead.
      version_incremental: A string representing the subcomponent of the
        build fingerprint used to identify the vbmeta in the transparency log.
      transparency_log_configs: List of TransparencyLogConfig used to request
        the inclusion proofs.
      manufacturer_key: Path to PEM file containting the key file used to sign
        messages sent to the transparency log servers.
      padding_size: If not 0, pads output so size is a multiple of the number.
      timeout: Duration in seconds before requests to the AFTL times out. A
        value of 0 or None means there will be no timeout.

    Returns:
      True if the inclusion proofs could be fetched from the transparency log
      servers and could be successfully validated; otherwise False.
    """
    # Retrieves vbmeta structure from given partition image.
    vbmeta_image, footer = self.get_vbmeta_image(vbmeta_image_path)

    # Fetches inclusion proofs for vbmeta structure from all transparency logs.
    aftl_image = AftlImage()
    for log_config in transparency_log_configs:
      try:
        icp_entry = self.request_inclusion_proof(log_config, vbmeta_image,
                                                 version_incremental,
                                                 manufacturer_key,
                                                 signing_helper,
                                                 signing_helper_with_files,
                                                 timeout)
        if not icp_entry.verify_vbmeta_image(vbmeta_image, log_config.pub_key):
          sys.stderr.write('The inclusion proof from {} could not be verified.'
                           '\n'.format(log_config.target))
        aftl_image.add_icp_entry(icp_entry)
      except AftlError as e:
        # The inclusion proof request failed. Continue and see if others will.
        sys.stderr.write('Requesting inclusion proof failed: {}.\n'.format(e))
        continue

    # Checks that the resulting AftlImage is sane.
    if aftl_image.image_header.icp_count != len(transparency_log_configs):
      sys.stderr.write('Valid inclusion proofs could only be retrieved from {} '
                       'out of {} transparency logs.\n'
                       .format(aftl_image.image_header.icp_count,
                               len(transparency_log_configs)))
      return False
    if not aftl_image.is_valid():
      sys.stderr.write('Resulting AftlImage structure is malformed.\n')
      return False
    keys = [log.pub_key for log in transparency_log_configs]
    if not aftl_image.verify_vbmeta_image(vbmeta_image, keys):
      sys.stderr.write('Resulting AftlImage inclusion proofs do not '
                       'validate.\n')
      return False

    # Writes original VBMeta image, followed by the AftlImage into the output.
    if footer:  # Checks if it is a chained partition.
      # TODO(b/147217370): Determine the best way to handle chained partitions
      # like the system.img. Currently, we only put the main vbmeta.img in the
      # transparency log.
      sys.stderr.write('Image has a footer and ICP for this format is not '
                       'implemented.\n')
      return False

    output.seek(0)
    output.write(vbmeta_image)
    encoded_aftl_image = aftl_image.encode()
    output.write(encoded_aftl_image)

    if padding_size > 0:
      total_image_size = len(vbmeta_image) + len(encoded_aftl_image)
      padded_size = avbtool.round_to_multiple(total_image_size, padding_size)
      padding_needed = padded_size - total_image_size
      output.write('\0' * padding_needed)

    sys.stdout.write('VBMeta image with AFTL image successfully created.\n')
    return True

  def _load_test_process_function(self, vbmeta_image_path,
                                  transparency_log_config,
                                  manufacturer_key,
                                  process_number, submission_count,
                                  preserve_icp_images, timeout, result_queue):
    """Function to be used by multiprocessing.Process.

    Arguments:
      vbmeta_image_path: Path to a vbmeta image file.
      transparency_log_config: A TransparencyLogConfig instance used to request
        an inclusion proof.
      manufacturer_key: Path to PEM file containting the key file used to sign
        messages sent to the transparency log servers.
      process_number: The number of the processes executing the function.
      submission_count: Number of total submissions to perform per
        process_count.
      preserve_icp_images: Boolean to indicate if the generated vbmeta image
        files with inclusion proofs should preserved in the $TMP directory.
      timeout: Duration in seconds before requests to the AFTL times out. A
        value of 0 or None means there will be no timeout.
      result_queue: Multiprocessing.Queue object for posting execution results.
    """
    for count in range(0, submission_count):
      version_incremental = 'aftl_load_testing_{}_{}'.format(process_number,
                                                             count)
      output_file = os.path.join(tempfile.gettempdir(),
                                 '{}_icp.img'.format(version_incremental))
      output = open(output_file, 'wb')

      # Instrumented section.
      start_time = time.time()
      result = self.make_icp_from_vbmeta(
          vbmeta_image_path=vbmeta_image_path,
          output=output,
          signing_helper=None,
          signing_helper_with_files=None,
          version_incremental=version_incremental,
          transparency_log_configs=[transparency_log_config],
          manufacturer_key=manufacturer_key,
          padding_size=0,
          timeout=timeout)
      end_time = time.time()

      output.close()
      if not preserve_icp_images:
        os.unlink(output_file)

      # Puts the result onto the result queue.
      execution_time = end_time - start_time
      result_queue.put((start_time, end_time, execution_time,
                        version_incremental, result))

  def load_test_aftl(self, vbmeta_image_path, output, transparency_log_config,
                     manufacturer_key,
                     process_count, submission_count, stats_filename,
                     preserve_icp_images, timeout):
    """Performs multi-threaded load test on a given AFTL and prints stats.

    Arguments:
      vbmeta_image_path: Path to a vbmeta image file.
      output: File to write the report to.
      transparency_log_config: A TransparencyLogConfig used to request an
        inclusion proof.
      manufacturer_key: Path to PEM file containting the key file used to sign
        messages sent to the transparency log servers.
      process_count: Number of processes used for parallel testing.
      submission_count: Number of total submissions to perform per
        process_count.
      stats_filename: Path to the stats file to write the raw execution data to.
        If None, it will be written to the $TMP directory.
      preserve_icp_images: Boolean to indicate if the generated vbmeta
        image files with inclusion proofs should preserved.
      timeout: Duration in seconds before requests to the AFTL times out. A
        value of 0 or None means there will be no timeout.

    Returns:
      True if the load tested succeeded without errors; otherwise False.
    """
    if process_count < 1 or submission_count < 1:
      sys.stderr.write('Values for --processes/--submissions '
                       'must be at least 1.\n')
      return False

    if not stats_filename:
      stats_filename = os.path.join(
          tempfile.gettempdir(),
          'load_test_p{}_s{}.csv'.format(process_count, submission_count))

    stats_file = None
    try:
      stats_file = open(stats_filename, 'wt')
      stats_file.write('start_time,end_time,execution_time,version_incremental,'
                       'result\n')
    except IOError as e:
      sys.stderr.write('Could not open stats file {}: {}.\n'
                       .format(stats_file, e))
      return False

    # Launch all the processes with their workloads.
    result_queue = multiprocessing.Queue()
    processes = set()
    execution_times = []
    results = []
    for i in range(0, process_count):
      p = multiprocessing.Process(
          target=self._load_test_process_function,
          args=(vbmeta_image_path, transparency_log_config,
                manufacturer_key, i, submission_count,
                preserve_icp_images, timeout, result_queue))
      p.start()
      processes.add(p)

    while processes:
      # Processes the results queue and writes these to a stats file.
      try:
        (start_time, end_time, execution_time, version_incremental,
         result) = result_queue.get(timeout=1)
        stats_file.write('{},{},{},{},{}\n'.format(start_time, end_time,
                                                   execution_time,
                                                   version_incremental, result))
        execution_times.append(execution_time)
        results.append(result)
      except queue.Empty:
        pass

      # Checks if processes are still alive; if not clean them up. join() would
      # have been nicer but we want to continously stream out the stats to file.
      for p in processes.copy():
        if not p.is_alive():
          processes.remove(p)

    # Prepares stats.
    executions = sorted(execution_times)
    execution_count = len(execution_times)
    median = 0

    # pylint: disable=old-division
    if execution_count % 2 == 0:
      median = (executions[execution_count // 2 - 1]
                + executions[execution_count // 2]) / 2
    else:
      median = executions[execution_count // 2]

    # Outputs the stats report.
    o = output
    o.write('Load testing results:\n')
    o.write('  Processes:               {}\n'.format(process_count))
    o.write('  Submissions per process: {}\n'.format(submission_count))
    o.write('\n')
    o.write('  Submissions:\n')
    o.write('    Total:                 {}\n'.format(len(executions)))
    o.write('    Succeeded:             {}\n'.format(results.count(True)))
    o.write('    Failed:                {}\n'.format(results.count(False)))
    o.write('\n')
    o.write('  Submission execution durations:\n')
    o.write('    Average:               {:.2f} sec\n'.format(
        sum(execution_times) / execution_count))
    o.write('    Median:                {:.2f} sec\n'.format(median))
    o.write('    Min:                   {:.2f} sec\n'.format(min(executions)))
    o.write('    Max:                   {:.2f} sec\n'.format(max(executions)))

    # Close the stats file.
    stats_file.close()
    if results.count(False):
      return False
    return True


class TransparencyLogConfig(object):
  """Class that gathers the fields representing a transparency log.

  Attributes:
    target: The hostname and port of the server in hostname:port format.
    pub_key: A PEM file that contains the public key of the transparency
      log server.
    api_key: The API key to use to interact with the transparency log
      server.
  """

  @staticmethod
  def from_argument(arg):
    """Build an object from a command line argument string.

    Arguments:
      arg: The transparency log as passed in the command line argument.
        It must be in the format: host:port,key_file[,api_key].

    Returns:
      The TransparencyLogConfig instance.

    Raises:
      argparse.ArgumentTypeError: If the format of arg is invalid.
    """
    api_key = None
    try:
      target, pub_key, *rest = arg.split(",", maxsplit=2)
    except ValueError:
      raise argparse.ArgumentTypeError("incorrect format for transparency log "
                                       "server, expected "
                                       "host:port,publickey_file.")
    if not target:
      raise argparse.ArgumentTypeError("incorrect format for transparency log "
                                       "server: host:port cannot be empty.")
    if not pub_key:
      raise argparse.ArgumentTypeError("incorrect format for transparency log "
                                       "server: publickey_file cannot be "
                                       "empty.")
    if rest:
      api_key = rest[0]
    return TransparencyLogConfig(target, pub_key, api_key)

  def __init__(self, target, pub_key, api_key=None):
    """Initializes a new TransparencyLogConfig object."""
    self.target = target
    self.pub_key = pub_key
    self.api_key = api_key


class AftlTool(avbtool.AvbTool):
  """Object for aftltool command-line tool."""

  def __init__(self):
    """Initializer method."""
    self.aftl = Aftl()
    super(AftlTool, self).__init__()

  def make_icp_from_vbmeta(self, args):
    """Implements the 'make_icp_from_vbmeta' sub-command."""
    args = self._fixup_common_args(args)
    return self.aftl.make_icp_from_vbmeta(args.vbmeta_image_path,
                                          args.output,
                                          args.signing_helper,
                                          args.signing_helper_with_files,
                                          args.version_incremental,
                                          args.transparency_log_servers,
                                          args.manufacturer_key,
                                          args.padding_size,
                                          args.timeout)

  def info_image_icp(self, args):
    """Implements the 'info_image_icp' sub-command."""
    return self.aftl.info_image_icp(args.vbmeta_image_path.name, args.output)

  def verify_image_icp(self, args):
    """Implements the 'verify_image_icp' sub-command."""
    return self.aftl.verify_image_icp(args.vbmeta_image_path.name,
                                      args.transparency_log_pub_keys,
                                      args.output)

  def load_test_aftl(self, args):
    """Implements the 'load_test_aftl' sub-command."""
    return self.aftl.load_test_aftl(args.vbmeta_image_path,
                                    args.output,
                                    args.transparency_log_server,
                                    args.manufacturer_key,
                                    args.processes,
                                    args.submissions,
                                    args.stats_file,
                                    args.preserve_icp_images,
                                    args.timeout)

  def run(self, argv):
    """Command-line processor.

    Arguments:
      argv: Pass sys.argv from main.
    """
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(title='subcommands')

    # Command: make_icp_from_vbmeta
    sub_parser = subparsers.add_parser('make_icp_from_vbmeta',
                                       help='Makes an ICP enhanced vbmeta image'
                                       ' from an existing vbmeta image.')
    sub_parser.add_argument('--output',
                            help='Output file name.',
                            type=argparse.FileType('wb'),
                            default=sys.stdout)
    sub_parser.add_argument('--vbmeta_image_path',
                            help='Path to a generate vbmeta image file.',
                            required=True)
    sub_parser.add_argument('--version_incremental',
                            help='Current build ID.',
                            required=True)
    sub_parser.add_argument('--manufacturer_key',
                            help='Path to the PEM file containing the '
                            'manufacturer key for use with the log.',
                            required=True)
    sub_parser.add_argument('--transparency_log_servers',
                            help='List of transparency log servers in '
                            'host:port,publickey_file[,api_key] format. The '
                            'publickey_file must be in the PEM format.',
                            nargs='+', type=TransparencyLogConfig.from_argument)
    sub_parser.add_argument('--padding_size',
                            metavar='NUMBER',
                            help='If non-zero, pads output with NUL bytes so '
                            'its size is a multiple of NUMBER (default: 0)',
                            type=avbtool.parse_number,
                            default=0)
    sub_parser.add_argument('--timeout',
                            metavar='SECONDS',
                            help='Timeout in seconds for transparency log '
                            'requests (default: 600 sec). A value of 0 means '
                            'no timeout.',
                            type=avbtool.parse_number,
                            default=600)
    self._add_common_args(sub_parser)
    sub_parser.set_defaults(func=self.make_icp_from_vbmeta)

    # Command: info_image_icp
    sub_parser = subparsers.add_parser(
        'info_image_icp',
        help='Show information about AFTL ICPs in vbmeta or footer.')
    sub_parser.add_argument('--vbmeta_image_path',
                            help='Path to vbmeta image for AFTL information.',
                            type=argparse.FileType('rb'),
                            required=True)
    sub_parser.add_argument('--output',
                            help='Write info to file',
                            type=argparse.FileType('wt'),
                            default=sys.stdout)
    sub_parser.set_defaults(func=self.info_image_icp)

    # Arguments for verify_image_icp.
    sub_parser = subparsers.add_parser(
        'verify_image_icp',
        help='Verify AFTL ICPs in vbmeta or footer.')

    sub_parser.add_argument('--vbmeta_image_path',
                            help='Image to verify the inclusion proofs.',
                            type=argparse.FileType('rb'),
                            required=True)
    sub_parser.add_argument('--transparency_log_pub_keys',
                            help='Paths to PEM files containing transparency '
                            'log server key(s). This must not be None.',
                            nargs='*',
                            required=True)
    sub_parser.add_argument('--output',
                            help='Write info to file',
                            type=argparse.FileType('wt'),
                            default=sys.stdout)
    sub_parser.set_defaults(func=self.verify_image_icp)

    # Command: load_test_aftl
    sub_parser = subparsers.add_parser(
        'load_test_aftl',
        help='Perform load testing against one AFTL log server. Note: This MUST'
        ' not be performed against a production system.')
    sub_parser.add_argument('--vbmeta_image_path',
                            help='Path to a generate vbmeta image file.',
                            required=True)
    sub_parser.add_argument('--output',
                            help='Write report to file.',
                            type=argparse.FileType('wt'),
                            default=sys.stdout)
    sub_parser.add_argument('--manufacturer_key',
                            help='Path to the PEM file containing the '
                            'manufacturer key for use with the log.',
                            required=True)
    sub_parser.add_argument('--transparency_log_server',
                            help='Transparency log server to test against in '
                            'host:port,publickey_file[,api_key] format. The '
                            'publickey_file must be in the PEM format.',
                            required=True,
                            type=TransparencyLogConfig.from_argument)
    sub_parser.add_argument('--processes',
                            help='Number of parallel processes to use for '
                            'testing (default: 1).',
                            type=avbtool.parse_number,
                            default=1)
    sub_parser.add_argument('--submissions',
                            help='Number of submissions to perform against the '
                            'log per process (default: 1).',
                            type=avbtool.parse_number,
                            default=1)
    sub_parser.add_argument('--stats_file',
                            help='Path to the stats file to write the raw '
                            'execution data to (Default: '
                            'load_test_p[processes]_s[submissions].csv.')
    sub_parser.add_argument('--preserve_icp_images',
                            help='Boolean flag to indicate if the generated '
                            'vbmeta image files with inclusion proofs should '
                            'preserved.',
                            action='store_true')
    sub_parser.add_argument('--timeout',
                            metavar='SECONDS',
                            help='Timeout in seconds for transparency log '
                            'requests (default: 0). A value of 0 means '
                            'no timeout.',
                            type=avbtool.parse_number,
                            default=0)
    sub_parser.set_defaults(func=self.load_test_aftl)

    args = parser.parse_args(argv[1:])
    try:
      success = args.func(args)
    except AttributeError:
      # This error gets raised when the command line tool is called without any
      # arguments. It mimics the original Python 2 behavior.
      parser.print_usage()
      print('aftltool: error: too few arguments')
      sys.exit(2)
    except AftlError as e:
      # Signals to calling tools that an unhandled exception occured.
      sys.stderr.write('Unhandled AftlError occured: {}\n'.format(e))
      sys.exit(2)

    if not success:
      # Signals to calling tools that the command has failed.
      sys.exit(1)

if __name__ == '__main__':
  tool = AftlTool()
  tool.run(sys.argv)
