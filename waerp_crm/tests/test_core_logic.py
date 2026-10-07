"""Pure-logic tests. Run anywhere (no bench needed):

    python -m unittest waerp_crm.tests.test_core_logic -v

They are also picked up by `bench run-tests --app waerp_crm`.
"""

import hashlib
import hmac
import unittest
from datetime import datetime, timedelta

from waerp_crm.core import logic as L


class TestPhones(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(L.normalize_phone("+91 98765-43210"), "919876543210")
        self.assertEqual(L.normalize_phone("9876543210", "91"), "919876543210")
        self.assertEqual(L.normalize_phone("09876543210", "91"), "919876543210")
        self.assertEqual(L.normalize_phone("0044 7700 900123"), "447700900123")
        self.assertEqual(L.normalize_phone(""), "")
        self.assertEqual(L.normalize_phone(None), "")

    def test_variants(self):
        self.assertEqual(L.phone_variants("919876543210"), ["919876543210"])
        self.assertIn("551191234567", L.phone_variants("5511991234567"))
        self.assertIn("5511991234567", L.phone_variants("551191234567"))


class TestSignature(unittest.TestCase):
    def test_valid_and_invalid(self):
        body = b'{"entry":[]}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(L.verify_meta_signature(body, sig, ["other", "s3cret"]))
        self.assertFalse(L.verify_meta_signature(body + b" ", sig, ["s3cret"]))
        self.assertFalse(L.verify_meta_signature(body, None, ["s3cret"]))
        self.assertFalse(L.verify_meta_signature(body, "sha1=abc", ["s3cret"]))
        self.assertFalse(L.verify_meta_signature(body, sig, [None, ""]))


class TestLadder(unittest.TestCase):
    def test_forward_only(self):
        M = L.MESSAGE_LADDER
        self.assertTrue(L.is_valid_transition("Sent", "Delivered", M))
        self.assertTrue(L.is_valid_transition("Sent", "Read", M))
        self.assertFalse(L.is_valid_transition("Read", "Delivered", M))
        self.assertFalse(L.is_valid_transition("Read", "Read", M))

    def test_failed_rules(self):
        M = L.MESSAGE_LADDER
        self.assertTrue(L.is_valid_transition("Queued", "Failed", M))
        self.assertTrue(L.is_valid_transition("Sent", "Failed", M))
        self.assertFalse(L.is_valid_transition("Delivered", "Failed", M))
        self.assertFalse(L.is_valid_transition("Failed", "Read", M))

    def test_recipient_counters(self):
        self.assertEqual(L.recipient_counter_columns("Pending", "Sent"), ["sent_count"])
        self.assertEqual(L.recipient_counter_columns("Sent", "Read"), ["delivered_count", "read_count"])
        self.assertEqual(L.recipient_counter_columns("Delivered", "Replied"), ["read_count", "replied_count"])
        self.assertEqual(L.recipient_counter_columns("Sent", "Failed"), ["failed_count"])


class TestKeywords(unittest.TestCase):
    def test_modes(self):
        self.assertTrue(L.matches_keyword("Hi, PRICE please", ["price"], "Whole Word"))
        self.assertFalse(L.matches_keyword("pricelist", ["price"], "Whole Word"))
        self.assertTrue(L.matches_keyword("pricelist", ["price"], "Contains"))
        self.assertTrue(L.matches_keyword(" Menu ", ["menu"], "Exact"))
        self.assertFalse(L.matches_keyword("menu please", ["menu"], "Exact"))
        self.assertFalse(L.matches_keyword("Menu", ["menu"], "Exact", case_sensitive=True))
        self.assertTrue(L.matches_keyword("need (help)", ["(help)"], "Whole Word"))  # regex chars are literal
        self.assertTrue(L.matches_keyword("नमस्ते दोस्त", ["नमस्ते"], "Whole Word"))
        self.assertFalse(L.matches_keyword("", ["x"]))


class TestInterpolateCompare(unittest.TestCase):
    def test_interpolate(self):
        out = L.interpolate("Hi {{contact.first_name}}, order {{ vars.order }} {{x.y}}",
                            {"contact": {"first_name": "Asha"}, "vars": {"order": "SO-1"}})
        self.assertEqual(out, "Hi Asha, order SO-1 ")

    def test_compare(self):
        self.assertTrue(L.compare("equals", "Yes", "yes"))
        self.assertTrue(L.compare("gt", "10", "9.5"))
        self.assertFalse(L.compare("gt", "abc", "1"))
        self.assertTrue(L.compare("exists", "x"))
        self.assertTrue(L.compare("not_exists", ""))
        self.assertTrue(L.compare("not_equals", None, "a"))
        self.assertTrue(L.compare("contains", "Hello World", "world"))

    def test_time_window(self):
        d = datetime(2026, 1, 5, 20, 0)
        self.assertTrue(L.in_time_window(d, "18:00-09:00"))
        self.assertFalse(L.in_time_window(d, "09:00-18:00"))
        self.assertTrue(L.in_time_window(d.replace(hour=10), "09:00-18:00"))
        self.assertFalse(L.in_time_window(d, "garbage"))

    def test_business_hours(self):
        monday_10 = datetime(2026, 10, 5, 10, 0)  # Monday
        rows = [{"day": "Monday", "from_time": timedelta(hours=9), "to_time": "18:00:00"}]
        self.assertTrue(L.within_business_hours(monday_10, rows))
        self.assertFalse(L.within_business_hours(monday_10.replace(hour=19), rows))
        self.assertFalse(L.within_business_hours(monday_10 + timedelta(days=1), rows))

    def test_wait(self):
        self.assertEqual(L.wait_seconds(2, "hours"), 7200)
        self.assertEqual(L.wait_seconds("bad", "days"), 60)


def _flow():
    return [
        {"node_key": "start", "node_type": "start", "config": {"next_node_key": "menu"}},
        {"node_key": "menu", "node_type": "send_buttons", "config": {
            "text": "Choose", "buttons": [
                {"reply_id": "sales", "title": "Sales", "next_node_key": "ask"},
                {"reply_id": "support", "title": "Support", "next_node_key": "human"}]}},
        {"node_key": "ask", "node_type": "collect_input", "config": {"prompt_text": "Name?", "var_key": "name",
                                                                       "next_node_key": "bye"}},
        {"node_key": "human", "node_type": "handoff", "config": {}},
        {"node_key": "bye", "node_type": "end", "config": {"text": "Thanks"}},
    ]


class TestFlowLogic(unittest.TestCase):
    def test_valid_flow(self):
        self.assertEqual(L.validate_flow(_flow(), "start"), [])

    def test_errors(self):
        nodes = _flow()
        nodes[1]["config"]["buttons"][0]["next_node_key"] = "missing"
        nodes.append({"node_key": "orphan", "node_type": "end", "config": {}})
        errs = L.validate_flow(nodes, "start")
        self.assertTrue(any("unknown node 'missing'" in e for e in errs))
        self.assertTrue(any("'orphan' is unreachable" in e for e in errs))
        self.assertTrue(L.validate_flow(nodes, "nope"))

    def test_button_limits(self):
        nodes = _flow()
        nodes[1]["config"]["buttons"] *= 2
        errs = L.validate_flow(nodes, "start")
        self.assertTrue(any("1-3 reply buttons" in e for e in errs))
        self.assertTrue(any("unique reply_id" in e for e in errs))

    def test_auto_loop_detected(self):
        nodes = [
            {"node_key": "start", "node_type": "start", "config": {"next_node_key": "a"}},
            {"node_key": "a", "node_type": "send_message", "config": {"text": "x", "next_node_key": "b"}},
            {"node_key": "b", "node_type": "condition", "config": {"true_node_key": "a", "false_node_key": "a"}},
        ]
        self.assertTrue(any("loop" in e for e in L.validate_flow(nodes, "start")))

    def test_waiting_loop_is_fine(self):
        nodes = _flow()
        nodes[4] = {"node_key": "bye", "node_type": "send_message", "config": {"text": "x", "next_node_key": "menu"}}
        self.assertEqual(L.validate_flow(nodes, "start"), [])

    def test_match_reply(self):
        cfg = _flow()[1]["config"]
        self.assertEqual(L.match_reply("send_buttons", cfg, "support", None), "human")
        self.assertEqual(L.match_reply("send_buttons", cfg, None, " sales "), "ask")
        self.assertEqual(L.match_reply("send_buttons", cfg, None, "2"), "human")
        self.assertIsNone(L.match_reply("send_buttons", cfg, None, "what?"))

    def test_fallback(self):
        self.assertEqual(L.decide_fallback("Reprompt Then Handoff", 1, 2), "reprompt")
        self.assertEqual(L.decide_fallback("Reprompt Then Handoff", 3, 2), "handoff")
        self.assertEqual(L.decide_fallback("Reprompt Then End", 3, 2), "end")
        self.assertEqual(L.decide_fallback("Ignore", 1, 2), "ignore")
        self.assertEqual(L.decide_fallback("Handoff", 1, 2), "handoff")

    def test_validate_input(self):
        self.assertTrue(L.validate_input("1,200.50", "number"))
        self.assertFalse(L.validate_input("12a", "number"))
        self.assertTrue(L.validate_input("a@b.co", "email"))
        self.assertFalse(L.validate_input("a@b", "email"))
        self.assertTrue(L.validate_input("SO-0042", "regex", r"SO-\d+"))
        self.assertFalse(L.validate_input("", "any"))


class TestAutomationTree(unittest.TestCase):
    def test_scopes(self):
        steps = [
            {"step_key": "c1", "step_type": "condition", "idx": 2},
            {"step_key": "s1", "step_type": "add_tag", "idx": 1},
            {"step_key": "y1", "parent_step_key": "c1", "branch": "yes", "idx": 3},
            {"step_key": "n1", "parent_step_key": "c1", "branch": "no", "idx": 4},
            {"step_key": "s2", "step_type": "send_message", "idx": 5},
        ]
        self.assertEqual([s["step_key"] for s in L.scope_steps(steps, None, None)], ["s1", "c1", "s2"])
        self.assertEqual([s["step_key"] for s in L.scope_steps(steps, "c1", "yes")], ["y1"])
        self.assertEqual([s["step_key"] for s in L.scope_steps(steps, "c1", "no")], ["n1"])


class TestTemplates(unittest.TestCase):
    def test_placeholders(self):
        self.assertEqual(L.count_placeholders("Hi {{1}}, total {{2}} {{ 3 }}"), 3)
        self.assertEqual(L.count_placeholders("none"), 0)
        self.assertEqual(L.render_template_preview("Hi {{1}} / {{2}}", ["A"]), "Hi A / {{2}}")

    def test_components(self):
        comps = L.build_template_components("DOCUMENT", ["A", ""], header_media={"id": "123", "filename": "x.pdf"})
        self.assertEqual(comps[0]["parameters"][0], {"type": "document", "document": {"id": "123", "filename": "x.pdf"}})
        self.assertEqual(comps[1]["parameters"][1]["text"], "-")  # Meta rejects empty params
        img = L.build_template_components("IMAGE", [], header_media={"link": "https://x/y.png", "filename": "y"})
        self.assertEqual(img[0]["parameters"][0], {"type": "image", "image": {"link": "https://x/y.png"}})
        self.assertEqual(L.build_template_components("NONE", []), [])


class TestAIHelpers(unittest.TestCase):
    def test_parse_output(self):
        self.assertEqual(L.parse_ai_output("[[HANDOFF]]"), ("", True))
        self.assertEqual(L.parse_ai_output(" Hello "), ("Hello", False))

    def test_turns(self):
        hist = [{"direction": "Outbound", "content": "promo"}, {"direction": "Inbound", "content": "hi"},
                {"direction": "Inbound", "content": "price?"}, {"direction": "Outbound", "content": "100"},
                {"direction": "Inbound", "content": ""}]
        self.assertEqual(L.to_chat_turns(hist), [{"role": "user", "content": "hi\nprice?"},
                                                 {"role": "assistant", "content": "100"}])

    def test_chunking(self):
        text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
        chunks = L.chunk_text(text, size=500, overlap=50)
        self.assertTrue(len(chunks) > 3)
        self.assertTrue(all(len(c) <= 560 for c in chunks))
        long = "x" * 2000
        self.assertTrue(len(L.chunk_text(long, 500, 50)) >= 4)
        self.assertEqual(L.chunk_text(""), [])

    def test_scores(self):
        self.assertAlmostEqual(L.cosine([1, 0], [1, 0]), 1.0)
        self.assertEqual(L.cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(L.cosine([], [1]), 0.0)
        self.assertGreater(L.lexical_score("refund policy days", "Our refund policy allows 7 days"), 0.9)
        self.assertEqual(L.lexical_score("zz", "abc"), 0.0)


if __name__ == "__main__":
    unittest.main()
