"""ERP Assistant parsing — pure tests (no bench needed):

    python -m unittest waerp_crm.tests.test_assistant_parse -v
"""

import unittest
from datetime import date

from waerp_crm.assistant.parse import extract_json, from_ai, is_ref_like, parse_command, parse_date


class TestParseCommand(unittest.TestCase):
    def p(self, text):
        return parse_command(text)

    def test_help_confirm_decline(self):
        self.assertEqual(self.p("help")["intent"], "help")
        self.assertEqual(self.p("")["intent"], "help")
        self.assertEqual(self.p("Yes")["intent"], "confirm")
        self.assertEqual(self.p("no")["intent"], "decline")

    def test_find_with_filters(self):
        r = self.p("find sales order customer: Harshit Dixit, date: 29-10-2026")
        self.assertEqual(r["intent"], "find")
        self.assertEqual(r["doctype"], "sales order")
        self.assertEqual(r["filters"], [("customer", "Harshit Dixit"), ("date", "29-10-2026")])

    def test_find_ranges_and_equals(self):
        r = self.p("find purchase order supplier=ABC Traders and from: 01-10-2026 , to: 31-10-2026")
        self.assertEqual(r["doctype"], "purchase order")
        self.assertEqual(r["filters"], [("supplier", "ABC Traders"), ("from", "01-10-2026"), ("to", "31-10-2026")])

    def test_find_without_filters(self):
        r = self.p("find work orders")
        self.assertEqual((r["intent"], r["doctype"], r["filters"]), ("find", "work orders", []))

    def test_show_and_numbers(self):
        self.assertEqual(self.p("show SAL-ORD-2026-00012")["ref"], "SAL-ORD-2026-00012")
        self.assertEqual(self.p("show 2")["ref"], "2")
        self.assertEqual((self.p("2")["intent"], self.p("2")["ref"]), ("show", "2"))
        self.assertEqual(self.p("SAL-ORD-2026-00012")["intent"], "show")

    def test_actions_on_current(self):
        for t in ("submit", "submit it", "cancel this"):
            r = self.p(t)
            self.assertIsNone(r["ref"], t)
        self.assertEqual(self.p("cancel SO-0001")["ref"], "SO-0001")
        self.assertEqual(self.p("print 3")["intent"], "pdf")

    def test_updates(self):
        r = self.p("set SAL-ORD-2026-00012 delivery date = 05-11-2026")
        self.assertEqual((r["intent"], r["ref"], r["updates"]),
                         ("update", "SAL-ORD-2026-00012", [("delivery date", "05-11-2026")]))
        r = self.p("set delivery date = 05-11-2026")
        self.assertEqual((r["ref"], r["updates"]), (None, [("delivery date", "05-11-2026")]))
        r = self.p("update delivery date of SO-0001 to 05-11-2026")
        self.assertEqual((r["ref"], r["updates"]), ("SO-0001", [("delivery date", "05-11-2026")]))

    def test_free_text_goes_to_ai(self):
        self.assertEqual(self.p("give me the sales order of harshit dixit on 29-10-2026")["intent"], "free")

    def test_ref_like(self):
        self.assertTrue(is_ref_like("SAL-ORD-2026-00012"))
        self.assertTrue(is_ref_like("#3"))
        self.assertFalse(is_ref_like("delivery"))


class TestDatesAndAI(unittest.TestCase):
    def test_dates(self):
        today = date(2026, 10, 31)
        cases = {"29-10-2026": "2026-10-29", "29/10/26": "2026-10-29", "2026-10-29": "2026-10-29",
                 "29 Oct 2026": "2026-10-29", "Oct 29, 2026": "2026-10-29", "today": "2026-10-31",
                 "yesterday": "2026-10-30", "31-02-2026": None, "soon": None}
        for raw, want in cases.items():
            self.assertEqual(parse_date(raw, today), want, raw)

    def test_ai_json(self):
        raw = 'Sure:\n```json\n{"intent":"find","doctype":"Sales Order","filters":{"customer":"Harshit Dixit","date":"2026-10-29"},"ref":null}\n```'
        cmd = from_ai(extract_json(raw))
        self.assertEqual(cmd["intent"], "find")
        self.assertEqual(cmd["filters"], [("customer", "Harshit Dixit"), ("date", "2026-10-29")])
        self.assertIsNone(cmd["ref"])
        self.assertEqual(from_ai({"intent": "delete everything"})["intent"], "unknown")
        self.assertIsNone(from_ai({"intent": "submit", "ref": "it"})["ref"])
        self.assertIsNone(extract_json("no json here"))


if __name__ == "__main__":
    unittest.main()
