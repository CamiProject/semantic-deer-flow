from app.gateway.services import _strip_server_run_metadata


def test_eval_metadata_is_reserved_for_trusted_eval_calls():
    metadata = {
        "client_label": "safe",
        "eval_run_id": "forged-run",
        "eval_case_id": "forged-case",
        "eval_trial_index": 9,
        "eval_dataset_hash": "forged-hash",
        "__private": "forged",
    }

    assert _strip_server_run_metadata(metadata) == {"client_label": "safe"}
    assert _strip_server_run_metadata(metadata, allow_eval_metadata=True) == {
        "client_label": "safe",
        "eval_run_id": "forged-run",
        "eval_case_id": "forged-case",
        "eval_trial_index": 9,
        "eval_dataset_hash": "forged-hash",
    }
