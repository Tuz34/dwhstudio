// Request identity is the sequence taken when a detail request starts.
// A list refresh reconciles that request, not whichever detail is on screen.

export function initialSelection() {
  return {
    nextSeq: 1,
    latestSeq: 0,
    requestedName: null,
    listedNames: [],
    detail: null,
    runDialog: null,
    runStatus: {},
  };
}

export function beginDetailRequest(state, mappingName) {
  const requestSeq = Number(state?.nextSeq) || 1;
  return {
    requestSeq,
    state: {
      ...state,
      listedNames: Array.isArray(state?.listedNames) ? state.listedNames.slice() : [],
      nextSeq: requestSeq + 1,
      latestSeq: requestSeq,
      requestedName: mappingName,
    },
  };
}

export function detailResponseApplies(state, response) {
  const names = Array.isArray(state?.listedNames) ? state.listedNames : [];
  return response?.requestSeq === state?.latestSeq && names.includes(response?.mappingName);
}

export function applyDetailResponse(state, response) {
  if (!detailResponseApplies(state, response)) return state;
  return {
    ...state,
    detail: response.detail,
    requestedName: response.mappingName,
  };
}

export function applyListResponse(state, listedNames) {
  const names = Array.isArray(listedNames) ? listedNames.slice() : [];
  const requested = state?.requestedName;
  const vanished = Boolean(requested) && !names.includes(requested);
  if (!vanished) {
    return { ...state, listedNames: names, cleared: false };
  }
  const previousStatus = state.runStatus || {};
  const runStatus = Object.prototype.hasOwnProperty.call(previousStatus, requested)
    ? { ...previousStatus }
    : previousStatus;
  if (runStatus !== previousStatus) delete runStatus[requested];
  const runDialog = state.runDialog && state.runDialog.mappingName === requested
    ? null
    : state.runDialog;
  return {
    ...state,
    listedNames: names,
    detail: null,
    runDialog,
    runStatus,
    cleared: true,
  };
}
