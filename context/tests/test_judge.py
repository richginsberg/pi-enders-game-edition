from context.judge import build_chat_request, build_judge_messages, parse_judge_output
from context.models import Kind


def test_build_judge_messages_shape():
    msgs = build_judge_messages("USER: hi\nASSISTANT: chose pgvector")
    assert msgs[0]["role"] == "system" and "JSON array" in msgs[0]["content"]
    assert msgs[1]["role"] == "user" and "chose pgvector" in msgs[1]["content"]


def test_build_chat_request_deterministic():
    req = build_chat_request("tier:s3", [{"role": "user", "content": "x"}])
    assert req == {"model": "tier:s3", "messages": [{"role": "user", "content": "x"}], "temperature": 0.0}


def test_parse_plain_json_array():
    out = '[{"kind": "decision", "text": "Chose pgvector"}, {"kind": "constraint", "text": "No committed keys"}]'
    facts = parse_judge_output(out)
    assert facts == [(Kind.DECISION, "Chose pgvector"), (Kind.CONSTRAINT, "No committed keys")]


def test_parse_strips_code_fence_and_prose():
    out = 'Here you go:\n```json\n[{"kind":"outcome","text":"Tests passed"}]\n```\nHope that helps!'
    assert parse_judge_output(out) == [(Kind.OUTCOME, "Tests passed")]


def test_parse_drops_unknown_kind_and_empty_text():
    out = '[{"kind":"bogus","text":"x"},{"kind":"handoff","text":"  "},{"kind":"handoff","text":"resume the flip"}]'
    assert parse_judge_output(out) == [(Kind.HANDOFF, "resume the flip")]


def test_parse_empty_array_and_garbage():
    assert parse_judge_output("[]") == []
    assert parse_judge_output("no json here") == []
    assert parse_judge_output("") == []


def test_parse_survives_malformed_json():
    assert parse_judge_output('[{"kind": "decision", "text": ]') == []
