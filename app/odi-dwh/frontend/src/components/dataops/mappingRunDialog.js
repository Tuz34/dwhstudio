// Run-dialog state for MappingList. Opening the dialog is always non-destructive:
// truncating the target is an explicit operator choice, never a default or a guess.

export const TRUNCATE_WARNING =
  'Every existing row in the target table will be deleted before this run.';

export function openRunDialogState(mappingName) {
  // A previous dialog's choice is deliberately ignored.
  return { mappingName, truncateTargetTable: false };
}

export function selectRunMode(state, truncateTargetTable) {
  return { ...state, truncateTargetTable: truncateTargetTable === true };
}

export function runModeWarning(state) {
  return state?.truncateTargetTable === true ? TRUNCATE_WARNING : null;
}

export function buildRunMappingRequest(state) {
  const mappingName = typeof state?.mappingName === 'string' ? state.mappingName.trim() : '';
  if (!mappingName) throw new Error('A mapping name is required to run a scenario.');
  return {
    mapping_name: mappingName,
    truncate_target_table: state.truncateTargetTable === true,
    approved: true,
  };
}
