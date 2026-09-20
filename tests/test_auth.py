"""Authentication verdicts read from the receiving server's own header (no network)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mailsweep.auth import assess, parse_authentication_results, site
from mailsweep.bridge import parse_headers

GOOGLE_PASS = ("mx.google.com; dkim=pass header.i=@united.com header.s=s1 header.b=AbCdEf; "
               "spf=pass (google.com: domain of bounce@united.com designates 1.2.3.4 as permitted sender) "
               "smtp.mailfrom=bounce@united.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=united.com")
OUTLOOK_PASS = ("spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=news.example.com; "
                "dkim=pass (signature was verified) header.d=news.example.com;"
                "dmarc=pass action=none header.from=news.example.com;compauth=pass reason=100")
PROTON_PASS = "mail.protonmail.ch; dmarc=pass (Used From Domain Record) header.from=united.com policy.dmarc=reject"


def hdr(ar, frm="United <no-reply@united.com>"):
    return {"authentication-results": ar, "from": frm} if ar is not None else {"from": frm}


@pytest.mark.parametrize("ar", [GOOGLE_PASS, OUTLOOK_PASS, PROTON_PASS])
def test_real_provider_formats_verify(ar):
    frm = "Shop <a@news.example.com>" if ar is OUTLOOK_PASS else "United <no-reply@united.com>"
    assert assess(hdr(ar, frm))[0] == "verified"


def test_outlook_form_without_authserv_id_is_parsed():
    r = parse_authentication_results(OUTLOOK_PASS)
    assert r["dmarc"] == "pass"
    assert r["dkim"] == [("pass", "news.example.com")] and r["spf"] == [("pass", "news.example.com")]


def test_comments_with_equals_signs_do_not_confuse_the_parser():
    r = parse_authentication_results(GOOGLE_PASS)
    assert r["dmarc"] == "pass" and r["spf"] == [("pass", "united.com")] and r["dkim"] == [("pass", "united.com")]


def test_dmarc_fail_is_failed():
    state, why = assess(hdr("mx.google.com; dkim=pass header.d=rectrac.com; spf=pass smtp.mailfrom=mail-east.rectrac.com; "
                            "dmarc=fail (p=REJECT) header.from=united.com"))
    assert state == "failed" and "forged" in why


def test_the_spoof_pattern_authenticated_as_someone_else_is_unverified_not_verified():
    # A real signature and SPF pass, but for rectrac.com, while the From line says united.com,
    # and no DMARC policy to fail it: exactly the case a naive dkim=pass check would wave through.
    state, why = assess(hdr("mx.example; dkim=pass header.d=rectrac.com; spf=pass smtp.mailfrom=rectrac.com; dmarc=none"))
    assert state == "unverified" and "rectrac.com, not united.com" in why


def test_no_dmarc_policy_but_dkim_aligned_with_the_from_domain_is_verified():
    state, why = assess(hdr("mx.example; dkim=pass header.d=mail.united.com; dmarc=none"))
    assert state == "verified" and "dkim passed for united.com" in why


def test_spf_aligned_also_counts():
    assert assess(hdr("mx.example; spf=pass smtp.mailfrom=bounce.united.com; dkim=none"))[0] == "verified"


def test_dkim_failure_for_the_right_domain_is_not_a_pass():
    assert assess(hdr("mx.example; dkim=fail header.d=united.com; dmarc=none"))[0] == "unverified"


def test_outlook_bestguesspass_counts_as_verified():
    assert assess(hdr("spf=pass smtp.mailfrom=united.com; dkim=none; dmarc=bestguesspass header.from=united.com"))[0] == "verified"


def test_no_header_is_unknown_not_unverified():
    assert assess(hdr(None)) == ("unknown", "no Authentication-Results header")


def test_garbage_header_does_not_crash_and_is_unverified():
    assert assess(hdr(";;; ==== dkim=  ; (((")) [0] == "unverified"


@pytest.mark.parametrize("host,want", [("click.enews.united.com", "united.com"), ("news.bbc.co.uk", "bbc.co.uk"),
                                       ("United.com", "united.com"), ("", "")])
def test_site(host, want):
    assert site(host) == want


def test_parse_headers_now_keeps_authentication_results_top_most_only():
    block = ("Authentication-Results: mx.google.com; dmarc=pass header.from=a.com\r\n"
             "Received: from x\r\n"
             "Authentication-Results: forged.example; dmarc=pass header.from=a.com\r\n"
             "From: A <a@a.com>\r\n")
    h = parse_headers(block)
    assert h["authentication-results"].startswith("mx.google.com")     # the receiver's, not a lower forged one
