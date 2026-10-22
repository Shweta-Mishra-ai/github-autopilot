"""Regression tests for the six-scanner harness and the repair scripts, one
per defect found in them while they were written. Each was a wrong table or
a wrong number before it was a test."""
import random
import re

import bench_multitool as mt
import exrex
import repair_gitleaks as rg
import repair_trufflehog as rt


class TestRustPatternsTranslate:
    def test_verbose_mode_drops_whitespace_inside_classes_too(self):
        # Rust's (?x) ignores whitespace in classes, Python's re.X does not
        py, flags = mt.rust_to_python("(?x)(?i)\n\\b ( [a-z0-9 ]{4} ) \\b\n")
        assert py == r"\b([a-z0-9]{4})\b" and flags & re.I

    def test_verbose_mode_drops_comment_groups(self):
        py, _ = mt.rust_to_python("(?x) (ab) (?# a comment ) c")
        assert py == "(ab)c"

    def test_spaced_quantifier_becomes_a_quantifier(self):
        # Nosey Parker writes `.{1, 5}` under (?x); without stripping, Python
        # reads it as the literal text "{1, 5}"
        py, _ = mt.rust_to_python("(?x) a .{1, 5} b")
        assert re.fullmatch(py, "a123b")


class TestKeywordHeads:
    def test_prefix_types_have_no_keyword(self):
        assert mt.keyword_heads(r"\b") == []

    def test_candidates_are_longest_first(self):
        heads = mt.keyword_heads(r"aws_?(?:secret)?\s*=")
        assert heads[0].startswith("aws_?(?:secret)?") and heads[-1] == "a"

    def test_choice_does_not_depend_on_a_lucky_draw(self):
        # '.?' rarely draws an identifier character; the candidate list must
        # be the same whatever the random state
        out = set()
        for seed in range(5):
            random.seed(seed)
            out.add(tuple(mt.keyword_heads(r"(?:github).?(?:api|app)?.?(?:id|key)")))
        assert len(out) == 1


class TestSixToolLabels:
    def test_providers_of_new_tables(self):
        assert mt.provider_of_type("NP:np.github.1") == "github"
        assert mt.provider_of_type("KF:betterleaks.github-pat") == "github"
        assert mt.provider_of_type("KF:veles.secrets/cloudflareapitoken") == "cloudflareapitoken"

    def test_rules_fired_by_new_tools(self):
        assert mt.label_matches("GL:github-pat", "noseyparker", "np.github.1")
        assert mt.label_matches("TH:github", "kingfisher", "betterleaks.github-pat")
        assert mt.label_matches("NP:np.github.1", "detectsecrets", "GitHub Token")
        assert not mt.label_matches("NP:np.github.1", "detectsecrets", "Slack Token")

    def test_lineage_excludes_kingfisher_types_from_gitleaks_comparisons(self):
        src = {"GL": ["GL:a"], "KF": ["KF:b"], "NP": ["NP:c"], "TH": ["TH:d"], "AP": []}
        assert mt.neutral_ids("gitleaks", "trufflehog", src) == ["NP:c"]
        assert mt.neutral_ids("noseyparker", "detectsecrets", src) == ["GL:a", "KF:b", "TH:d"]


class TestAlphabet:
    def test_escaped_quantifier_character_is_a_literal(self):
        # '\+' lost its '+' to the quantifier stripper, leaving a lone '\'
        assert "+" in rg.alphabet(r"endr\+[a-z]{4}", False)

    def test_mid_pattern_flag_makes_the_alphabet_case_insensitive(self):
        # p8e-(?i)[a-z0-9]{32}: the flag was read as atoms '?' and 'i'
        a = rg.alphabet(r"p8e-(?i)[a-z0-9]{32}", False)
        assert {"A", "Z", "-", "p"} <= a and "?" not in a

    def test_scoped_flag_group(self):
        assert "C" in rg.alphabet(r"s\.(?i:[a-z0-9]{24})", False)

    def test_negated_class_round_trips(self):
        cls = rg.negated_class(set("ab-]\\"))
        rx = re.compile(cls)
        assert not any(rx.fullmatch(c) for c in "ab-]\\") and rx.fullmatch("c")


class TestRepairsOnlyWiden:
    """Neither repair may lose a detection the original makes."""

    def test_gitleaks_r2_keeps_every_original_terminator(self):
        # hashicorp-tf-password's alphabet contains '"', which the original
        # terminator accepts; complementing the bare alphabet rejected it
        alpha = rg.alphabet(r'"[a-z0-9=_\-]{8,20}"', False)
        term = re.compile(rg.negated_class(alpha - rg.TERM_CHARS))
        assert all(term.fullmatch(c) for c in "'\"` ;|")

    def test_trufflehog_repair_accepts_whatever_word_boundary_accepts(self):
        body = r"\b(r8_[0-9A-Za-z-_]{4})\b"
        new, nonword = rt.repair_literal(body, False)
        old_rx, new_rx = re.compile(body), re.compile(new)
        for s in ("r8_abcd", 'r8_abcd"', "r8_abcd-x", "r8_abc-", 'r8_abc-"', "r8_abcde"):
            if old_rx.search(s):
                assert new_rx.search(s), s
        assert new_rx.search('r8_abc-"') and not old_rx.search('r8_abc-"')
        assert nonword == ["-"]

    def test_word_alphabets_are_left_alone(self):
        assert rt.repair_literal(r"\b(ghp_[A-Za-z0-9_]{36})\b", False) is None


class TestReproducerValuesAreRandom:
    def test_one_generator_per_value(self):
        # random.Random(7).choice() inside the comprehension drew the same
        # character 34 times: 'hf_uuuu...', which the scanners' entropy
        # filters discard
        import importlib.util
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "reproduce_matrix", os.path.join(here, "..", "upstream", "reproduce_matrix.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for v in (m.HEX, m.HF, m.R8):
            assert len(set(v)) > len(v) // 3, v


def test_exrex_is_seeded_by_random():
    random.seed(3)
    a = exrex.getone("[a-z]{12}")
    random.seed(3)
    assert exrex.getone("[a-z]{12}") == a
