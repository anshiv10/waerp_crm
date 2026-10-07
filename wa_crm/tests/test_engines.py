"""End-to-end engine tests against a real site (Meta Graph API is mocked).

    bench --site <test-site> set-config allow_tests true
    bench --site <test-site> run-tests --app wa_crm
"""

import itertools
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from wa_crm.whatsapp.inbound import process_payload

PHONE_ID = "PNID-TEST-1"
_counter = itertools.count(1)
SENT = []


def fake_request(self, method, path, **kwargs):
    if path.endswith("/messages"):
        body = kwargs.get("json") or {}
        if body.get("status") == "read":
            return {"success": True}
        SENT.append(body)
        return {"messages": [{"id": f"wamid.OUT{next(_counter)}"}]}
    return {}


def inbound(wa_id, text=None, wamid=None, reply_id=None, name="Test User"):
    msg = {"from": wa_id, "id": wamid or f"wamid.IN{next(_counter)}", "timestamp": str(int(now_datetime().timestamp())),
           "type": "text", "text": {"body": text}}
    if reply_id:
        msg = {**msg, "type": "interactive",
               "interactive": {"type": "button_reply", "button_reply": {"id": reply_id, "title": reply_id}}}
        msg.pop("text")
    return {"entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE_ID, "display_phone_number": "1"},
        "contacts": [{"wa_id": wa_id, "profile": {"name": name}}], "messages": [msg]}}]}]}


def status_payload(wamid, status):
    return {"entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PHONE_ID},
        "statuses": [{"id": wamid, "status": status, "timestamp": "1", "recipient_id": "x"}]}}]}]}


@patch("wa_crm.whatsapp.graph.GraphClient._request", fake_request)
class TestEngines(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.db.set_single_value("WA CRM Settings", {"auto_create_lead": 0, "mirror_inbound_media": 0,
                                                        "default_country_code": "91"})
        if not frappe.db.exists("WA Account", "Test Account"):
            frappe.get_doc({"doctype": "WA Account", "account_name": "Test Account", "enabled": 1, "is_default": 1,
                            "phone_number_id": PHONE_ID, "waba_id": "WABA1", "api_version": "v23.0",
                            "access_token": "tok", "app_secret": "sec", "verify_token": "ver",
                            "assignment_mode": "None"}).insert(ignore_permissions=True)

    def setUp(self):
        SENT.clear()

    def _conv(self, wa_id):
        wc = frappe.db.get_value("WA Contact", {"wa_id": wa_id}, "name")
        return wc, f"Test Account-{wc}"

    def test_ingest_is_idempotent(self):
        p = inbound("919900000001", "hello", wamid="wamid.DUP1")
        process_payload(p)
        process_payload(p)  # Meta retry
        wc, conv = self._conv("919900000001")
        self.assertTrue(wc)
        self.assertEqual(frappe.db.count("WA Message", {"conversation": conv, "direction": "Inbound"}), 1)
        self.assertEqual(frappe.db.get_value("WA Conversation", conv, "unread_count"), 1)

    def test_status_ladder(self):
        process_payload(inbound("919900000002", "hi"))
        _, conv = self._conv("919900000002")
        from wa_crm.whatsapp.outbound import send_text
        msg = send_text(conv, "reply")
        process_payload(status_payload(msg.message_id, "read"))
        process_payload(status_payload(msg.message_id, "delivered"))  # late, must not regress
        self.assertEqual(frappe.db.get_value("WA Message", msg.name, "status"), "Read")

    def test_window_blocks_freeform(self):
        process_payload(inbound("919900000003", "hi"))
        _, conv = self._conv("919900000003")
        frappe.db.set_value("WA Conversation", conv, "last_inbound_at", add_to_date(now_datetime(), hours=-25))
        from wa_crm.whatsapp.outbound import OutboundBlocked, send_text
        with self.assertRaises(OutboundBlocked):
            send_text(conv, "late reply")

    def test_flow_run(self):
        flow = frappe.get_doc({
            "doctype": "WA Flow", "title": "Test Menu", "status": "Active", "trigger_type": "Keyword",
            "keywords": "testmenu", "match_type": "Exact", "entry_node_key": "start", "nodes": [
                {"node_key": "start", "node_type": "start", "config": json.dumps({"next_node_key": "menu"})},
                {"node_key": "menu", "node_type": "send_buttons", "config": json.dumps({"text": "Pick", "buttons": [
                    {"reply_id": "b_sales", "title": "Sales", "next_node_key": "ask"}]})},
                {"node_key": "ask", "node_type": "collect_input",
                 "config": json.dumps({"prompt_text": "Name?", "var_key": "name", "next_node_key": "bye"})},
                {"node_key": "bye", "node_type": "end", "config": json.dumps({"text": "Thanks {{vars.name}}"})},
            ]}).insert(ignore_permissions=True)
        process_payload(inbound("919900000004", "testmenu"))
        wc, _ = self._conv("919900000004")
        run = frappe.get_last_doc("WA Flow Run", filters={"wa_contact": wc})
        self.assertEqual((run.status, run.current_node_key), ("Active", "menu"))
        self.assertEqual(SENT[-1]["type"], "interactive")
        process_payload(inbound("919900000004", reply_id="b_sales"))
        run.reload()
        self.assertEqual(run.current_node_key, "ask")
        process_payload(inbound("919900000004", "Asha"))
        run.reload()
        self.assertEqual(run.status, "Completed")
        self.assertEqual(json.loads(run.vars)["name"], "Asha")
        self.assertEqual(SENT[-1]["text"]["body"], "Thanks Asha")
        flow.db_set("status", "Archived")

    def test_automation_wait_and_resume(self):
        auto = frappe.get_doc({
            "doctype": "WA Automation", "title": "Price reply", "is_active": 1, "trigger_type": "Keyword Match",
            "keywords": "pricetest", "match_type": "Whole Word", "steps": [
                {"step_key": "s1", "step_type": "send_message", "config": json.dumps({"text": "Sending prices"})},
                {"step_key": "w1", "step_type": "wait", "config": json.dumps({"amount": 1, "unit": "minutes"})},
                {"step_key": "s2", "step_type": "add_tag", "config": json.dumps({"tag": "Price Asked"})},
            ]}).insert(ignore_permissions=True)
        process_payload(inbound("919900000005", "need pricetest now"))
        wc, _ = self._conv("919900000005")
        log = frappe.get_last_doc("WA Automation Log", filters={"automation": auto.name})
        self.assertEqual(log.status, "Waiting")
        pe = frappe.get_last_doc("WA Pending Execution", filters={"automation": auto.name})
        pe.db_set("run_at", add_to_date(now_datetime(), minutes=-1))
        from wa_crm.engines.automations import process_pending
        process_pending()
        log.reload()
        self.assertEqual(log.status, "Success")
        self.assertTrue(frappe.db.exists("WA Contact Tag", {"parent": wc, "tag": "Price Asked"}))
        auto.db_set("is_active", 0)

    def test_opt_out(self):
        process_payload(inbound("919900000006", "STOP"))
        wc, _ = self._conv("919900000006")
        self.assertEqual(frappe.db.get_value("WA Contact", wc, "opt_status"), "Opted Out")
