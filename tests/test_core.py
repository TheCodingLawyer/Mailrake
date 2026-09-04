"""Tests for the logic that decides what gets unsubscribed.

These cover the two functions where a bug is expensive: the sensitivity
classifier (a false negative unsubscribes you from your bank) and the
List-Unsubscribe parser (a wrong target sends a request to the wrong place).
"""
from __future__ import annotations

import pytest

from gmail_unsub.core.classifier import check_sender
from gmail_unsub.core.unsubscribe import (
    choose_target,
    describe_targets,
    parse_list_unsubscribe,
)
from gmail_unsub.store.settings import DEFAULT_SENSITIVE_KEYWORDS

KW = DEFAULT_SENSITIVE_KEYWORDS


class TestClassifier:
    @pytest.mark.parametrize("email,name", [
        ("alerts@chase.com", "Chase Bank"),
        ("noreply@irs.gov", "IRS"),
        ("service@paypal.com", "PayPal"),
        ("no-reply@accounts.google.com", "Google Account"),
        ("billing@example.com", ""),
    ])
    def test_flags_sensitive(self, email, name):
        assert check_sender(email, name, KW).is_sensitive

    @pytest.mark.parametrize("email,name", [
        ("news@substack.com", "Some Newsletter"),
        ("hello@figma.com", "Figma"),
        ("deals@retailer.example", "Weekly Deals"),
    ])
    def test_allows_ordinary_senders(self, email, name):
        assert not check_sender(email, name, KW).is_sensitive

    def test_matches_on_word_boundaries_not_substrings(self):
        # "gov" must not fire on "governor" or "lovegov"; this boundary rule
        # is what keeps the sensitive list from swallowing normal senders.
        assert not check_sender("news@governorsball.com", "Governors Ball", ["gov"]).is_sensitive
        assert check_sender("x@my.gov.uk", "", ["gov"]).is_sensitive

    def test_empty_keyword_list_flags_nothing(self):
        assert not check_sender("alerts@chase.com", "Chase", []).is_sensitive

    def test_reports_which_keywords_matched(self):
        result = check_sender("statement@chase.com", "Chase Bank", KW)
        assert set(result.reasons) >= {"chase", "bank", "statement"}


class TestListUnsubscribeParsing:
    def test_parses_https_and_mailto(self):
        header = "<https://ex.com/u?t=abc>, <mailto:unsub@ex.com>"
        targets = parse_list_unsubscribe(header)
        assert [t.method for t in targets] == ["https", "mailto"]
        assert targets[0].value == "https://ex.com/u?t=abc"
        assert targets[1].value == "unsub@ex.com"

    def test_strips_mailto_query_params(self):
        targets = parse_list_unsubscribe("<mailto:u@ex.com?subject=unsubscribe>")
        assert targets[0].value == "u@ex.com"

    def test_empty_header_yields_nothing(self):
        assert parse_list_unsubscribe("") == []
        assert choose_target([], False) is None
        assert describe_targets([], False) == "none"

    def test_prefers_https_over_mailto(self):
        targets = parse_list_unsubscribe("<mailto:u@ex.com>, <https://ex.com/u>")
        assert choose_target(targets, has_one_click_post=False).method == "https"

    def test_falls_back_to_mailto_when_only_option(self):
        targets = parse_list_unsubscribe("<mailto:u@ex.com>")
        assert choose_target(targets, False).method == "mailto"

    def test_one_click_is_described_distinctly(self):
        targets = parse_list_unsubscribe("<https://ex.com/u>")
        assert "one-click" in describe_targets(targets, True)
        assert "one-click" not in describe_targets(targets, False)


class TestInflectedKeywords:
    """Real sender addresses are overwhelmingly plural.

    1.x matched on a strict word boundary, so `accounts@`, `receipts@` and
    `statements@` all slipped past the sensitivity guard. These lock the fix.
    """

    @pytest.mark.parametrize("email,name,expected", [
        ("no-reply@accounts.google.com", "Google Accounts", "account"),
        ("noreply-accounts@google.com", "Google", "account"),
        ("receipts@shop.example", "Receipts", "receipt"),
        ("invoices@vendor.example", "Invoices", "invoice"),
        ("payments@x.example", "Payments", "payment"),
        ("banking@x.example", "Banking", "bank"),
        ("taxes@x.example", "Taxes", "tax"),
        ("alerts@x.example", "Alerts", "alert"),
        ("confirmed@x.example", "Confirmed", "confirm"),
    ])
    def test_inflections_are_matched(self, email, name, expected):
        result = check_sender(email, name, KW)
        assert result.is_sensitive
        assert expected in result.reasons

    @pytest.mark.parametrize("email,name", [
        # The inflection allowance must not become a substring match.
        ("news@governorsball.com", "Governors Ball"),
        ("hello@figma.com", "Figma"),
        ("news@substack.com", "Some Newsletter"),
        ("team@taxidermy.example", "Taxidermy Weekly"),
        ("hi@accountancyjobs.example", "Accountancy Jobs"),
    ])
    def test_unrelated_senders_stay_clean(self, email, name):
        assert not check_sender(email, name, KW).is_sensitive
