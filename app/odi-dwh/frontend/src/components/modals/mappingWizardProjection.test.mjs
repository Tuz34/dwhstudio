import assert from 'node:assert/strict';
import test from 'node:test';

import {
  neutralSkipDetail,
  wizardHeadlineDetail,
  wizardStepTone,
} from './mappingWizardRequests.js';


const success = {
  overall_status: 'SUCCESS',
  log: [
    { step: 'audit_preservation', status: 'SKIPPED', detail: 'Audit preservation does not apply to this load.' },
    { step: 'create_skey', status: 'SKIPPED', detail: 'SKEY not requested' },
    { step: 'populate_lineage', status: 'SKIPPED', detail: 'mapping generation failed.' },
    { step: 'create_mapping', status: 'SUCCESS', detail: 'Created' },
  ],
};

test('a skipped step is neutral and is not the headline', () => {
  assert.equal(wizardHeadlineDetail(success, success.log), '');
  assert.equal(wizardStepTone('SKIPPED'), 'neutral');
  assert.equal(wizardStepTone('FAILED'), 'failure');
  assert.equal(
    neutralSkipDetail(success.log[0].detail),
    'Audit preservation does not apply to this load.',
  );
  assert.equal(neutralSkipDetail(success.log[1].detail), 'SKEY not requested');
  assert.equal(neutralSkipDetail(success.log[2].detail), 'Step skipped.');
  assert.equal(neutralSkipDetail('mapping generation failed.').includes('failed'), false);
});

test('a genuine failure still supplies the headline and the failure tone', () => {
  const failed = {
    overall_status: 'FAILED',
    error: 'mapping generation failed.',
    log: [
      { step: 'create_skey', status: 'SKIPPED', detail: 'SKEY not requested' },
      { step: 'pipeline', status: 'FAILED', detail: 'mapping generation failed.' },
    ],
  };
  assert.equal(wizardHeadlineDetail(failed, failed.log), 'mapping generation failed.');
  assert.equal(wizardStepTone('FAILED'), 'failure');
  assert.equal(neutralSkipDetail(failed.log[0].detail), 'SKEY not requested');
});
