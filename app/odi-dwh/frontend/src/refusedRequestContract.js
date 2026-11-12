// Mirrored from datamart_contract.py and routes/error_responses.py.
// The cross-language test detects drift. Server response text is not rendered.
import { DATAMART_TYPE2_ONLY_WARNING } from './datamartContract.js';

export const DATAMART_REQUIRES_TYPE2_CODE = 'datamart_requires_type2';
export const AUDIT_PRESERVATION_REJECTED_CODE = 'audit_preservation_rejected';
export const AUDIT_PRESERVATION_REJECTED_MESSAGE =
  "The target table's audit column types are not supported for audit preservation. Nothing was created.";

const REFUSED_REQUEST_SENTENCES = Object.freeze({
  [DATAMART_REQUIRES_TYPE2_CODE]: DATAMART_TYPE2_ONLY_WARNING,
  [AUDIT_PRESERVATION_REJECTED_CODE]: AUDIT_PRESERVATION_REJECTED_MESSAGE,
});

export function refusedRequestMessage(code) {
  if (typeof code !== 'string' || !Object.prototype.hasOwnProperty.call(REFUSED_REQUEST_SENTENCES, code)) {
    return null;
  }
  return REFUSED_REQUEST_SENTENCES[code];
}
