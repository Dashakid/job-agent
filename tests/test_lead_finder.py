import unittest

import lead_finder
import outreach_agent

BARE_SITE = """<html><head><title>Smith &amp; Co CPA</title></head><body>
<p>Call us at 555-0100. Email <a href="mailto:office@smithcpa.com">office@smithcpa.com</a></p>
<footer>&copy; 2019 Smith CPA</footer></body></html>"""

MODERN_SITE = """<html><head><meta name="viewport" content="width=device-width">
<title>Bright Dental</title></head><body>
<a href="https://calendly.com/bright">Book online</a>
<form><input type="email" name="email"><textarea name="message"></textarea></form>
<script src="https://widget.podium.com/x.js"></script>
<footer>&copy; 2026 Bright Dental</footer></body></html>"""


class AuditTests(unittest.TestCase):
    def test_bare_site_reports_every_gap(self):
        audit = lead_finder.audit_html(
            "https://smithcpa.com", "http://smithcpa.com/", BARE_SITE, current_year=2026
        )
        joined = " | ".join(audit.issues)
        self.assertIn("book", joined)
        self.assertIn("contact or quote form", joined)
        self.assertIn("chat or text-us", joined)
        self.assertIn("phones", joined)
        self.assertIn("HTTPS", joined)
        self.assertIn("2019", joined)
        self.assertEqual(audit.emails, ["office@smithcpa.com"])
        self.assertEqual(audit.title, "Smith & Co CPA")

    def test_modern_site_has_no_gaps(self):
        audit = lead_finder.audit_html(
            "https://brightdental.com", "https://brightdental.com/", MODERN_SITE, current_year=2026
        )
        self.assertEqual(audit.issues, [])

    def test_contact_page_form_counts(self):
        contact_page = '<form><textarea name="message"></textarea></form>'
        audit = lead_finder.audit_html(
            "https://a.com", "https://a.com/", BARE_SITE, contact_page, current_year=2026
        )
        self.assertFalse(any("contact or quote form" in issue for issue in audit.issues))

    def test_search_form_is_not_a_contact_form(self):
        self.assertFalse(lead_finder.has_contact_form(
            '<form role="search"><input type="email"></form>'
        ))

    def test_junk_emails_are_dropped_and_same_domain_first(self):
        html = "x@sentry.io a@gmail.com logo@2x.png info@acme.com"
        self.assertEqual(lead_finder.extract_emails(html, "acme.com"), ["info@acme.com", "a@gmail.com"])


class OverpassTests(unittest.TestCase):
    def test_parse_dedupes_by_domain_and_needs_name_and_site(self):
        elements = [
            {"tags": {"name": "Acme Tax", "website": "acmetax.com", "addr:city": "Tampa",
                      "addr:state": "FL", "phone": "+1 555"}},
            {"tags": {"name": "Acme Tax Downtown", "website": "https://www.acmetax.com/"}},
            {"tags": {"name": "No Site LLC"}},
            {"tags": {"website": "https://nameless.com"}},
        ]
        businesses = lead_finder.parse_overpass_elements(elements)
        self.assertEqual(len(businesses), 1)
        self.assertEqual(businesses[0]["website"], "https://acmetax.com")
        self.assertEqual(businesses[0]["city"], "Tampa, FL")

    def test_split_bbox_quarters_the_area(self):
        tiles = lead_finder.split_bbox((0.0, 0.0, 2.0, 4.0))
        self.assertEqual(tiles[0], (0.0, 0.0, 1.0, 2.0))
        self.assertEqual(tiles[3], (1.0, 2.0, 2.0, 4.0))

    def test_query_covers_every_tag_and_website_key(self):
        query = lead_finder.build_overpass_query(['office="accountant"'], (1.0, 2.0, 3.0, 4.0))
        self.assertIn('node[office="accountant"]["website"](1.00000,2.00000,3.00000,4.00000);', query)
        self.assertIn('way[office="accountant"]["contact:website"]', query)


class TargetTests(unittest.TestCase):
    def _audit(self, **overrides):
        audit = lead_finder.SiteAudit(url="https://acme.com/", reachable=True,
                                      issues=["no chat or text-us option on the site"])
        for key, value in overrides.items():
            setattr(audit, key, value)
        return audit

    def test_email_target_loads_in_outreach_agent(self):
        target = lead_finder.build_target(
            {"name": "Acme Tax", "city": "Tampa, FL"}, self._audit(emails=["info@acme.com"]),
            "accountants",
        )
        self.assertEqual(target["contacts"][0]["channel"], "email")
        self.assertIn("Site check", target["context"])
        self.assertEqual(outreach_agent.classify_target_type(target), "small_business")
        loaded = outreach_agent.normalize_target_record(target)
        self.assertEqual(loaded["contacts"][0]["email"], "info@acme.com")

    def test_falls_back_to_contact_page_then_skips(self):
        by_form = lead_finder.build_target(
            {"name": "Acme"}, self._audit(contact_page_url="https://acme.com/contact"), "lawyers"
        )
        self.assertEqual(by_form["contacts"][0]["channel"], "contact_form")
        self.assertIsNone(lead_finder.build_target({"name": "Acme"}, self._audit(), "lawyers"))

    def test_merge_skips_known_domains_and_companies(self):
        existing = [{"company": "Acme", "website": "https://www.acme.com"}]
        new = [
            {"company": "Acme Two", "website": "https://acme.com/"},
            {"company": "acme", "website": "https://other.com"},
            {"company": "Fresh", "website": "https://fresh.com"},
        ]
        merged, added = lead_finder.merge_targets(existing, new)
        self.assertEqual(added, 1)
        self.assertEqual(merged[-1]["company"], "Fresh")


class FitTests(unittest.TestCase):
    def _audit(self, url, title):
        return lead_finder.SiteAudit(url=url, reachable=True, title=title)

    def test_labs_suppliers_universities_are_not_a_fit(self):
        for name, title in (("Ceramics Dental Lab", "Lab"), ("CAD/CAM Center", "Equipment Supplier"),
                            ("UF Hialeah Dental Center", "UF Health Hialeah"),
                            ("Face To Face Mental Health Services", "Serving Our Community")):
            reason = lead_finder.fit_problem({"name": name}, self._audit("https://x.com/", title),
                                             "https://x.com")
            self.assertEqual(reason, "not an owner-run small business", name)

    def test_redirect_to_other_domain_is_skipped(self):
        reason = lead_finder.fit_problem(
            {"name": "Alternative Tax Services"},
            self._audit("https://www.guardianaccountinggroup.com/merger", "Merger"),
            "https://alternativetax.com",
        )
        self.assertIn("redirects", reason)

    def test_hijacked_domain_is_skipped(self):
        reason = lead_finder.fit_problem(
            {"name": "Zambrano Orthodontics"},
            self._audit("https://www.miamidadeorthodontist.com/", "爱游戏体育app官网登录入口"),
            "https://www.miamidadeorthodontist.com",
        )
        self.assertIn("does not mention", reason)

    def test_real_practice_passes(self):
        self.assertEqual(lead_finder.fit_problem(
            {"name": "South Gables Dental"},
            self._audit("https://southgablesdental.com/", "Coral Gables Dentist - South Gables Dental"),
            "https://southgablesdental.com",
        ), "")

    def test_rename_redirect_keeping_the_name_passes(self):
        self.assertEqual(lead_finder.fit_problem(
            {"name": "Smile Creators"},
            self._audit("https://smilecreators.com/", "Smile Creators | Miami Dentist"),
            "https://smilecreatorsmiami.com",
        ), "")

    def test_generic_trade_word_in_title_is_enough(self):
        self.assertEqual(lead_finder.fit_problem(
            {"name": "Florida Dental Care of Miller"},
            self._audit("https://fldentalcaremiami.com/", "Dentist Miami, FL | Emergency Dental Care"),
            "https://fldentalcaremiami.com",
        ), "")

    def test_animal_hospital_is_a_small_business(self):
        self.assertIsNone(lead_finder.NOT_A_FIT_RE.search("Coral Way Animal Hospital"))
        self.assertTrue(lead_finder.NOT_A_FIT_RE.search("Baptist Hospital"))

    def test_schedule_your_consultation_counts_as_booking(self):
        self.assertTrue(lead_finder.BOOKING_RE.search("Schedule your personal consultation"))
        self.assertTrue(lead_finder.BOOKING_RE.search("Request an appointment"))


class SmallBusinessPromptTests(unittest.TestCase):
    def _prompt(self, target_type):
        return outreach_agent.build_outreach_prompt(
            "Acme Tax", {"name": outreach_agent.PLACEHOLDER_CONTACT_NAME}, [],
            "## Proof-of-Work\n- **SMS Outreach Automation:** texting system.",
            "Site check of their public website found: no chat or text-us option on the site.",
            "reverse_audit", target_type=target_type,
        )

    def test_small_business_gets_owner_rules(self):
        prompt = self._prompt("small_business")
        self.assertIn("AUDIENCE OVERRIDE", prompt)
        self.assertIn("Cite SMS Outreach Automation", prompt)

    def test_other_types_do_not(self):
        self.assertNotIn("AUDIENCE OVERRIDE", self._prompt("software"))


    def test_signature_has_name_city_and_opt_out(self):
        signature = outreach_agent.small_business_signature()
        self.assertIn("stop", signature)
        self.assertGreaterEqual(len(signature.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()


class BrowserAndConfidenceTests(unittest.TestCase):
    def _target(self, **overrides):
        target = {
            "company": "Smith Dental", "website": "https://smithdental.com/",
            "audit_findings": ["no chat or text-us option on the site",
                               "no way to book or request an appointment online; visitors have to call"],
            "contacts": [{"name": "Leadership Contact", "channel": "email",
                          "email": "info@smithdental.com"}],
        }
        target.update(overrides)
        return target

    def test_reconcile_keeps_only_gaps_the_rendered_page_confirms(self):
        verified, disproven = lead_finder.reconcile_findings(["a", "b", "c"], ["a", "c", "new"])
        self.assertEqual((verified, disproven), (["a", "c"], ["b"]))

    def test_browser_confirmed_own_domain_email_ranks_high(self):
        score = lead_finder.score_target(self._target(browser_checked=True))
        self.assertEqual((score["score"], score["label"]), (65, "high"))

    def test_third_gap_does_not_outrank_own_domain_email(self):
        three_offdomain = self._target(
            browser_checked=True,
            audit_findings=["a", "b", "c"],
            contacts=[{"name": "x", "channel": "email", "email": "web@vendor.com"}],
        )
        two_own = self._target(browser_checked=True)
        self.assertLess(lead_finder.score_target(three_offdomain)["score"],
                        lead_finder.score_target(two_own)["score"])

    def test_opening_claim_must_be_a_confirmed_gap(self):
        target = {
            "audit_findings": ["no chat or text-us option on the site"],
            "disproven_findings": ["no way to book or request an appointment online; visitors have to call"],
        }
        booking = outreach_agent.unconfirmed_claims("I noticed there's no way to book online. More.", target)
        self.assertEqual(len(booking), 1)
        self.assertIn("actually has", booking[0])
        self.assertEqual(outreach_agent.unconfirmed_claims(
            "I noticed there's no chat on your site. I can add booking too.", target), [])
        unchecked = outreach_agent.unconfirmed_claims(
            "I noticed your site isn't set up for phones.", {"audit_findings": ["no chat or text-us"]})
        self.assertIn("did not confirm", unchecked[0])

    def test_unchecked_offdomain_disproven_ranks_low(self):
        target = self._target(
            contacts=[{"name": "x", "channel": "email", "email": "smith@gmail.com"}],
            disproven_findings=["no contact or quote form"],
        )
        score = lead_finder.score_target(target)
        self.assertEqual(score["label"], "low")
        self.assertIn("not their domain", "; ".join(score["reasons"]))

    def test_chain_and_emptied_leads_are_skip(self):
        self.assertEqual(lead_finder.score_target(self._target(company="Banfield Pet Hospital"))["label"],
                         "skip")
        self.assertEqual(lead_finder.score_target(self._target(audit_findings=[]))["label"], "skip")
        directory = self._target(company="Choice MD", site_title="ChoiceMD | Find Local Healthcare")
        self.assertEqual(lead_finder.score_target(directory)["label"], "skip")

    def test_sort_puts_most_confident_first(self):
        weak = self._target(company="Weak", contacts=[{"name": "x", "channel": "contact_form"}])
        strong = self._target(company="Strong", browser_checked=True)
        self.assertEqual([t["company"] for t in lead_finder.score_and_sort([weak, strong])],
                         ["Strong", "Weak"])

    def test_review_details_flag_disproven_gaps(self):
        target = self._target(browser_checked=True, disproven_findings=["no contact or quote form"])
        target["confidence"] = lead_finder.score_target(target)
        details = outreach_agent.format_review_details(target, target["contacts"][0])
        self.assertIn("https://smithdental.com/", details)
        self.assertIn("[confirmed] no chat", details)
        self.assertIn("[WRONG - their site has this] no contact or quote form", details)
        self.assertIn("Send to: info@smithdental.com", details)

    def test_skip_ranked_lead_blocks_approval(self):
        target = self._target(company="Morgan & Morgan")
        target["confidence"] = lead_finder.score_target(target)
        self.assertIn("chain", outreach_agent.review_block_reason(target))
        self.assertEqual(outreach_agent.review_block_reason({"confidence": {"label": "high"}}), "")
