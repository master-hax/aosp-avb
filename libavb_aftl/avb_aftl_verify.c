/*
 * Copyright (C) 2020 The Android Open Source Project
 *
 * Permission is hereby granted, free of charge, to any person
 * obtaining a copy of this software and associated documentation
 * files (the "Software"), to deal in the Software without
 * restriction, including without limitation the rights to use, copy,
 * modify, merge, publish, distribute, sublicense, and/or sell copies
 * of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be
 * included in all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
 * BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
 * ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 * CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

/* still need avb_aftl_slot_verify, take params from avb_slot_verify &
   call find_aftl_descriptor and avb_aftl_verify_descriptor on them */
#include <stdio.h>

#include <libavb/avb_slot_verify.h>
#include <libavb/avb_util.h>

#include "libavb_aftl/avb_aftl_types.h"
#include "libavb_aftl/avb_aftl_util.h"
#include "libavb_aftl/avb_aftl_validate.h"
#include "libavb_aftl/avb_aftl_verify.h"

AvbSlotVerifyResult aftl_slot_verify(AvbSlotVerifyData* asv_data,
                                     uint8_t* key_bytes,
                                     size_t key_size) {
  size_t i;
  size_t aftl_descriptor_size;
  uint8_t* current_aftl_blob;
  AvbSlotVerifyResult result = AVB_SLOT_VERIFY_RESULT_OK;

  avb_assert(asv_data != NULL);
  avb_assert(key_bytes != NULL);
  avb_assert(key_size == AVB_AFTL_PUB_KEY_SIZE);
  /* Walk through each vbmeta blob in the AvbSlotVerifyData struct. */
  if (asv_data->vbmeta_images != NULL) {
    for (i = 0; i < asv_data->num_vbmeta_images; i++) {
      aftl_descriptor_size = asv_data->vbmeta_images[i].vbmeta_size;
      current_aftl_blob = avb_aftl_find_aftl_descriptor(
          asv_data->vbmeta_images[i].vbmeta_data, &aftl_descriptor_size);
      if (current_aftl_blob != NULL) {
        /* get key and size*/
        result =
            avb_aftl_verify_descriptor(asv_data->vbmeta_images[i].vbmeta_data,
                                       asv_data->vbmeta_images[i].vbmeta_size,
                                       current_aftl_blob,
                                       aftl_descriptor_size,
                                       key_bytes,
                                       key_size);
        if (result != AVB_SLOT_VERIFY_RESULT_OK) break;
      }
    }
  }

  return result;
}

uint8_t* avb_aftl_find_aftl_descriptor(uint8_t* vbmeta_blob,
                                       size_t* vbmeta_size) {
  size_t i;
  /* todo: find a better way of doing this, there *is* avb_strstr */
  for (i = 0; i < *vbmeta_size - 4; i++) {
    if ((vbmeta_blob[i] == 'A') && (vbmeta_blob[i + 1] == 'F') &&
        (vbmeta_blob[i + 2] == 'T') && (vbmeta_blob[i + 3] == 'L')) {
      *vbmeta_size -= i;
      return &(vbmeta_blob[i]);
    }
  }
  *vbmeta_size = 0;
  return NULL;
}

/* look at the flow in the readme and match that with error codes*/
AvbSlotVerifyResult avb_aftl_verify_descriptor(uint8_t* cur_vbmeta_data,
                                               size_t cur_vbmeta_size,
                                               uint8_t* aftl_blob,
                                               size_t aftl_size,
                                               uint8_t* key_bytes,
                                               size_t key_num_bytes) {
  size_t i;
  AftlDescriptor* aftl_descriptor;
  AvbSlotVerifyResult result = AVB_SLOT_VERIFY_RESULT_OK;

  /* Attempt to parse the AftlDescriptor pointed to by aftl_blob. */
  aftl_descriptor = parse_aftl_descriptor(aftl_blob, aftl_size);
  if (!aftl_descriptor) {
    return AVB_SLOT_VERIFY_RESULT_ERROR_VERIFICATION;
  }

  /* Now that a valid AftlDescriptor has been parsed, attempt to verify
     the inclusion proof(s) in three steps. */
  for (i = 0; i < aftl_descriptor->header.icp_count; i++) {
    /* 1. Ensure that the vbmeta hash stored in the AftlIcpEntry matches
       the one that represents the partition. */
    if (avb_aftl_verify_vbmeta_hash(
            cur_vbmeta_data, cur_vbmeta_size, aftl_descriptor->entries[i])) {
      /* 2. Ensure that the root hash of the Merkle tree representing
         the transparency log entry matches the one stored in the
         AftlIcpEntry. */
      if (avb_aftl_verify_icp_root_hash(aftl_descriptor->entries[i])) {
        /* 3. Verify the signature using the transparency log public
           key stored on device. */
        if (avb_aftl_verify_entry_signature(
                key_bytes, key_num_bytes, aftl_descriptor->entries[i])) {
          /* Everything passed verification, set status to OK. */
          result = AVB_SLOT_VERIFY_RESULT_OK;
        } else {
          avb_errorv(
              "AFTL signature verification failed on entry ", i, "\n", NULL);
          result = AVB_SLOT_VERIFY_RESULT_ERROR_VERIFICATION;
        }
      } else {
        avb_errorv(
            "AFTL root hash verification failed on entry ", i, "\n", NULL);
        result = AVB_SLOT_VERIFY_RESULT_ERROR_VERIFICATION;
      }
    } else {
      avb_errorv(
          "AFTL vbmeta hash verification failed on entry ", i, "\n", NULL);
      result = AVB_SLOT_VERIFY_RESULT_ERROR_VERIFICATION;
    }
    if (result != AVB_SLOT_VERIFY_RESULT_OK) break;
  }

  free_aftl_descriptor(aftl_descriptor);
  return result;
}
