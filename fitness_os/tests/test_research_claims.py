"""Regression tests: claim patterns match only what the (corrected) evidence summaries justify."""
import pytest

from fitness_os import research


def v(claim):
    return research.screen_claim(claim)["verdict"]


@pytest.mark.parametrize("claim,verdict", [
    ("Do crunches to burn belly fat", "not_supported"),
    ("spot reduction works for thighs", "not_supported"),
    ("You must eat within 30 minutes after your workout", "overstated"),
    ("The anabolic window closes fast", "overstated"),
    ("Protein shake within 30 minutes of your workout is essential", "overstated"),
    ("Going into starvation mode stops fat loss", "overstated"),
    ("More sets is always better for muscle", "overstated"),
    ("There is no limit to sets that help", "overstated"),
    ("You should train each muscle once a week only", "context_dependent"),
    ("Creatine damages your kidneys", "overstated"),
    ("Sleeping 5 hours is enough for me", "not_supported"),
])
def test_intended_claims_still_match(claim, verdict):
    assert v(claim) == verdict


@pytest.mark.parametrize("claim", [
    "I lose belly fat with diet and exercise",          # general exercise is not abdominal spot work
    "I stretch 30 minutes after training",              # no protein/eating context
    "Absorbing nutrients matters for belly fat loss",   # 'abs' inside a word
    "Sleeping 5 hours is not enough",                   # negated: must not be flagged as the claim
    "I slept 14 hours after the race and felt fine",    # 14 is not 4
    "Cardio once a week is best for my schedule",       # not about muscles/split
    "More sets is better",                              # not an 'always' claim
    "Creatine may harm people with kidney disease",     # clinical caution the summary itself gives
    "Creatine harms the liver",                         # liver is not covered by the summary
    "Eat every 3 hours to boost metabolism",            # no source addresses meal frequency
])
def test_loose_matches_no_longer_fire(claim):
    assert v(claim) in ("unverified", "no_sources"), claim


def test_meal_frequency_rule_removed_and_not_cited_to_protein_paper():
    r = research.screen_claim("Eat every 3 hours to boost metabolism")
    assert r["verdict"] != "not_supported"
    assert not any(pat for pat, _, _, ids in research.CLAIMS if "meal" in pat and "metabolism" in pat)


def test_every_claim_source_exists_and_regexes_compile():
    import re
    for pat, verdict, why, ids in research.CLAIMS:
        re.compile(pat, re.I)
        assert all(i in research.BY_ID for i in ids)
        assert verdict == "no_sources" or ids


def test_explanations_do_not_overstate_the_summaries():
    by = {tuple(ids): why for _, _, why, ids in research.CLAIMS}
    assert "diminishing" not in by[("schoenfeld2017volume",)]
    assert "matters more than the split" not in by[("schoenfeld2016freq",)]


def test_unconfirmed_details_are_flagged_in_data_and_output():
    for sid in ("acsm2009", "helms2014", "who2020"):
        d = research.BY_ID[sid]["detail_check"]
        assert d["unverified"] and d["confirmed"]
        assert d["verified_on"] == "2026-11-21" and d["source"].strip()
        assert research._brief(research.BY_ID[sid])["unverified_details"] == d["unverified"]
    who = research.BY_ID["who2020"]["summary"]
    assert "two or more days" not in who                # not confirmable from the abstract
    assert "8 to 12 repetition-max" not in research.BY_ID["acsm2009"]["summary"]


# ---------------------------------------------------------------- denial guard (false positives) ----------------------------------------------------------------
@pytest.mark.parametrize("claim", [
    "Spot reduction does not work",
    "Spot reduction is a myth",
    "The anabolic window is a myth",
    "I do not believe in the anabolic window",
    "Starvation mode is a myth",
    "Creatine doesn't harm kidneys",
    "Crunches do not burn belly fat",
    "Creatine does not damage your kidneys",
])
def test_claims_that_deny_the_myth_are_not_flagged_as_the_myth(claim):
    assert v(claim) in ("unverified", "no_sources"), claim


@pytest.mark.parametrize("claim,verdict", [                       # false negatives: plain phrasings must still be caught
    ("Spot reduction works for thighs", "not_supported"),
    ("You can lose belly fat with crunches", "not_supported"),
    ("Eat protein within 30 minutes after your workout", "overstated"),
    ("Your metabolism will shut down if you eat too little", "overstated"),
    ("Creatine damages kidneys", "overstated"),
    ("Sleeping 4 hours is fine", "not_supported"),
    ("Is it true more sets is always better?", "overstated"),
])
def test_plain_myths_still_flagged(claim, verdict):
    assert v(claim) == verdict


def test_detox_rule_is_not_guarded_and_explains_no_source():
    r = research.screen_claim("Detox teas flush out toxins and burn fat")
    assert r["verdict"] == "no_sources" and r["sources"] == []


def test_sanitised_unknown_claim_is_never_true():
    assert v("Lifting makes you taller") in ("unverified", "no_sources")


# ---------------------------------------------------------------- verification status ----------------------------------------------------------------
def test_every_citation_has_an_honest_status_and_no_entry_claims_full_verification():
    for s in research.CORPUS:
        assert s["verified_status"] in ("partial", "recorded"), s["id"]
        assert s["verification_note"] and s["doi"] and s["verified_on"]
        assert s["doi"].startswith("10.") and s["url"] == "https://doi.org/" + s["doi"]
        b = research._brief(s)
        assert b["verified_status"] == s["verified_status"] and b["verification_note"]
    for sid in ("acsm2009", "helms2014", "who2020"):
        assert research.BY_ID[sid]["verified_status"] == "partial"
    assert sum(s["verified_status"] == "partial" for s in research.CORPUS) == 3


def test_status_reaches_screen_output():
    src = research.screen_claim("Spot reduction works for thighs")["sources"][0]
    assert src["verified_status"] == "recorded" and src["verification_note"]
