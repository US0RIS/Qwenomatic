import pytest

from runtime.agent import MalformedOutput, parse_output


def test_plain_json():
    p = parse_output('{"actions": [{"tool": "market.offer", "args": {"segment": "s", "price": 5}}]}', max_actions=3)
    assert p.actions == [{"tool": "market.offer", "args": {"segment": "s", "price": 5}}]


def test_qwen_think_block_and_fence():
    text = '<think>considering pricing...</think>\nHere you go:\n```json\n{"actions": [], "memory": "m"}\n```'
    p = parse_output(text, max_actions=3)
    assert p.actions == [] and p.memory == "m"


def test_action_cap():
    acts = ",".join('{"tool": "memory.note", "args": {"text": "x"}}' for _ in range(5))
    p = parse_output('{"actions": [%s]}' % acts, max_actions=2)
    assert len(p.actions) == 2 and p.dropped_actions == 3


def test_claims_are_extracted_not_trusted():
    p = parse_output('{"actions": [], "revenue": 5000}', max_actions=3)
    assert p.claims == {"revenue": 5000}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", '{"actions": "offer"}',
                                  '{"actions": [{"args": {}}]}', '{"actions": [{"tool": "x", "args": [1]}]}'])
def test_malformed(text):
    with pytest.raises(MalformedOutput):
        parse_output(text, max_actions=3)


def test_qwen_agent_prompt_disables_thinking():
    from runtime.agent.prompts import build_messages

    genotype = {
        "strategy_prompt": "Test a legitimate offer.",
        "workflow": "offer_first",
        "target": {"segment": "local-services"},
        "pricing_parameters": {"price": 19.0},
        "tool_preferences": ["market.offer"],
        "planning_parameters": {"temperature": 0.2, "max_tokens": 512, "survey_every": 4},
    }
    messages = build_messages(genotype, [], [], [], 0, 3)
    assert messages[0]["content"].rstrip().endswith("/no_think")
