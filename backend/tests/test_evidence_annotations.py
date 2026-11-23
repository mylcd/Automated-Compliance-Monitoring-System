"""Failure highlights must point into the selected immutable evidence only."""
import json

import pytest

from app.seed import import_sources, load_sources
from test_phase3 import client, factory, keyed, rewrite_snapshot, scan, seeded, settings


def inspect(client, result, dataset):
    reference = next(e for e in result["evidence"] if e["dataset_type"] == dataset)
    path = f"/api/evidence/{reference['id']}"
    response = client.get(path + "/annotations", params={"result_id": result["id"]})
    assert response.status_code == 200, response.text
    return response.json(), client.get(path + "/content").json(), path


def value_at(data, pointer):
    for part in pointer.strip('/').split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def test_missing_consent_has_red_absence_explanation_and_only_failed_activity(client):
    assessment, results = scan(client)
    result = keyed(results)[('LOY-01', 'R001')]
    note, data, _ = inspect(client, result, 'consent_events')
    assert note['assessment_id'] == assessment['id']
    assert len(note['issues']) == 1 and note['issues'][0]['missing']
    assert note['issues'][0]['customer_id'] == 'C001-01'
    assert note['issues'][0]['purpose_id'] == 'PUR-MARKETING'
    assert note['highlights'] == []  # No fabricated consent record, no unrelated customer marked.
    note, data, _ = inspect(client, result, 'customer_activities')
    assert len(note['highlights']) == 1
    assert value_at(data, note['highlights'][0]['path'])['id'] == 'ACT-C001-01'


def test_payment_exact_fields_and_latest_assessment_context(client):
    assessment, results = scan(client)
    result = keyed(results)[('PAY-01', 'R002')]
    note, data, path = inspect(client, result, 'payment_configurations')
    assert len(note['highlights']) == 1
    assert note['highlights'][0]['path'].endswith('/settings_json/tls_enabled')
    assert value_at(data, note['highlights'][0]['path']) is False
    all_notes = client.get(path + '/annotations').json()
    assert all_notes['assessment_id'] == assessment['id']
    assert {h['control_id'] for h in all_notes['highlights']} == {'PAY-01', 'PAY-03'}
    filtered = client.get(path+'/annotations', params={'assessment_id':assessment['id'], 'restaurant_id':'R001'}).json()
    assert filtered['highlights'] == [] and filtered['issues'] == []
    passing, _, _ = inspect(client, keyed(results)[('PAY-01', 'R001')], 'payment_configurations')
    assert passing['highlights'] == [] and passing['issues'] == []


@pytest.mark.parametrize('control,field,value', [('PAY-02','mfa_enabled',False), ('PAY-02','password_length',8),
    ('PAY-03','audit_logging_enabled',False), ('PAY-04','installed_patch_version','2026.01'), ('PAY-05','prohibited_storage_detected',True)])
def test_payment_nested_values_match_failure(client, seeded, factory, control, field, value):
    def change(rows):
        observed = next(r for r in rows if r['asset_id']=='R004-payment')['settings_json']
        (observed['accounts'][0] if control=='PAY-02' else observed)[field] = value
    rewrite_snapshot(seeded, factory, 'payment_configurations', change)
    _, results = scan(client, ['R004'], ['PCI-DSS'])
    note, data, _ = inspect(client, keyed(results)[(control,'R004')], 'payment_configurations')
    assert len(note['highlights']) == 1
    assert note['highlights'][0]['path'].endswith('/'+field)
    assert value_at(data, note['highlights'][0]['path']) == value
    assert '/accounts/0/' in note['highlights'][0]['path'] if control=='PAY-02' else '/accounts/' not in note['highlights'][0]['path']


@pytest.mark.parametrize('control,dataset,identifier,changes', [
    ('LOY-03','loyalty_records','LR-C004-01',{'retention_due_at':'2026-11-16T12:00:00Z'}),
    ('LOY-04','customer_requests','REQ-R004',{'status':'open','completed_at':None})])
def test_retention_and_request_deadlines(client, seeded, factory, control, dataset, identifier, changes):
    rewrite_snapshot(seeded, factory, dataset, lambda rows: next(r for r in rows if r['id']==identifier).update(changes))
    _, results = scan(client, ['R004'], ['GDPR'])
    note, data, _ = inspect(client, keyed(results)[(control,'R004')], dataset)
    assert note['highlights']
    for highlight in note['highlights']:
        value_at(data, highlight['path'])
        assert data['records'][int(highlight['path'].split('/')[2])]['id'] == identifier


def test_withdrawal_and_unauthorized_action_annotations(client, seeded):
    bundles = load_sources(scenario='unauthorized-modification')
    bundles['loyalty']['datasets']['consent_events'].append(dict(id='CE-WITHDRAW-TEST', customer_id='C001-02', purpose_id='PUR-MARKETING', event_type='withdraw', occurred_at='2026-10-01T10:00:00Z', notice_version=None))
    import_sources(seeded, bundles)
    _, results = scan(client, ['R001'], ['GDPR'])
    note, data, _ = inspect(client, keyed(results)[('LOY-02','R001')], 'consent_events')
    assert any(value_at(data, h['path'])=='withdraw' for h in note['highlights'])
    assert all(data['records'][int(h['path'].split('/')[2])]['customer_id']=='C001-02' for h in note['highlights'])
    note, data, _ = inspect(client, keyed(results)[('LOY-05','R001')], 'employee_access_events')
    assert any(value_at(data, h['path'])=='customer.update' for h in note['highlights'])
    assert all(data['records'][int(h['path'].split('/')[2])]['id']=='EV-UNAUTHORIZED-001' for h in note['highlights'])


def test_old_failure_highlights_survive_corrected_import_and_new_passing_result(client, seeded):
    _, results = scan(client)
    old = keyed(results)[('PAY-01','R002')]
    before, data, path = inspect(client, old, 'payment_configurations')
    import_sources(seeded, load_sources(scenario='remediated'))
    _, after_results = scan(client)
    after, _, _ = inspect(client, old, 'payment_configurations')
    assert before == after
    passing, _, _ = inspect(client, keyed(after_results)[('PAY-01','R002')], 'payment_configurations')
    assert passing['highlights'] == []
    unrelated = next(e for e in old['evidence'] if e['dataset_type']=='assets')
    assert client.get(f"/api/evidence/{unrelated['id']}/annotations", params={'result_id':'RES-does-not-exist'}).status_code==404
    assert client.get(path+'/annotations', params={'assessment_id':'A-does-not-exist'}).status_code==404


def test_annotation_rejects_tampering_and_requires_authentication(client, seeded):
    _, results = scan(client)
    note, data, path = inspect(client, keyed(results)[('PAY-01','R002')], 'payment_configurations')
    reference = next(e for e in client.get('/api/evidence?dataset=payment_configurations').json()['items'])
    target = seeded.evidence_dir/reference['relative_path']
    target.write_text('{}')
    assert client.get(path+'/annotations').status_code==409
    client.cookies.clear()
    assert client.get(path+'/annotations').status_code==401


@pytest.mark.parametrize('field,value', [('notice_version',None),('occurred_at','2026-10-11T10:00:00Z')])
def test_consent_existing_bad_field_is_highlighted_without_marking_other_customers(client, seeded, factory, field, value):
    rewrite_snapshot(seeded, factory, 'consent_events', lambda rows: next(r for r in rows if r['customer_id']=='C001-02').update({field:value}))
    _, results = scan(client, ['R001'], ['GDPR'])
    note, data, _ = inspect(client, keyed(results)[('LOY-01','R001')], 'consent_events')
    assert len(note['highlights'])==1
    assert note['highlights'][0]['path'].endswith('/'+field)
    assert value_at(data,note['highlights'][0]['path'])==value
