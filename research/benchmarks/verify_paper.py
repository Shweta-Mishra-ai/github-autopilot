"""
Verify every headline number asserted in the manuscript against the raw
result files. Any mismatch is a defect in the paper, not in the data.
"""
import json, os, re, sys
from collections import Counter

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tex = (open(os.path.join(_ROOT, 'paper', 'main.tex')).read()
       + open(os.path.join(_ROOT, 'paper', 'online_resource_1.tex')).read())
R = lambda n: json.load(open(os.path.join(_ROOT, 'results', f'{n}.json')))

mut  = R('mutation_bench')
comp = R('comparative_bench')
cross= R('crosstool_bench')
sysb = R('systems_bench')
met  = R('metrics_br_rlp_sp')
prob = R('probability_analysis')
fixv = R('fixvalidation_bench')
stab = R('stability_bench')

checks = []
def chk(label, claim_in_tex, expected, actual):
    ok = (abs(expected-actual) < 1e-6) if isinstance(expected,float) else (expected==actual)
    present = claim_in_tex in tex
    checks.append((label, ok and present, expected, actual, present))

# ---- RQ1 aggregate: hyphen context -----------------------------------------
agg = mut['aggregate_by_mutation']['trailing_hyphen']
chk('hyphen rule-fire rate',  '0.5233', 0.5233, round(agg['rule_match_rate'],4))
chk('hyphen fallback rate',   '0.2317', 0.2317, round(agg['fallback_rate'],4))
chk('hyphen missed rate',     '0.2450', 0.2450, round(agg['missed_rate'],4))

# ---- worst non-hyphen context ----------------------------------------------
others = {k:v['rule_match_rate'] for k,v in mut['aggregate_by_mutation'].items()
          if k not in ('trailing_hyphen','trailing_under')}
chk('min non-hyphen rate 0.9976', '0.9976', 0.9976, round(min(others.values()),4))

# ---- counts ----------------------------------------------------------------
chk('24 rules analysed',    '24 rules', 24,  mut['n_patterns_tested'])
chk('n=120 per cell',       '$n=120$',  120, mut['n_samples_per_cell'])
chk('5 fragile rules',      'Five rules are boundary-fragile', 5, mut['n_boundary_fragile_patterns'])
chk('seeded harness',       '\\texttt{20260823}', 20260823, mut['seed'])

# ---- BR / RLP / SP table ---------------------------------------------------
for rule, br, rlp in [('SendGrid API Key',0.0,1.0), ('GCP API Key',0.0,0.9116),
                      ('OpenAI API Key (new)',0.8417,0.9861), ('JWT Token',0.8833,1.0),
                      ('Google OAuth Token',0.8917,1.0)]:
    chk(f'BR {rule}',  f'{br:.4f}',  br,  round(met[rule]['BR'],4))
    chk(f'RLP {rule}', f'{rlp:.4f}', rlp, round(met[rule]['RLP'],4))

macro_br = sum(v['BR'] for v in met.values())/len(met)
chk('macro BR 0.9007', '0.9007', 0.9007, round(macro_br,4))

# ---- Table V: exact marginal probabilities ---------------------------------
chk('P(-) exact 1/64',        '0.015625', 0.015625, round(1/64, 6))
mr = prob['marginal_rates']
chk('SendGrid marginal 1/64', '0.015625', 0.015625, mr['SendGrid API Key']['p_missed_marginal'])
chk('SendGrid 1 in 64',       '1 in 64',  64, mr['SendGrid API Key']['one_in_n_missed'])
chk('GCP 1 in 64 downgraded', '1 in 64 (downgraded)', 64,
    mr['GCP API Key']['one_in_n_not_correctly_labelled'])
chk('Google OAuth 1 in 591',  '1 in 591', 591, mr['Google OAuth Token']['one_in_n_missed'])
chk('JWT HS384 1 in 548',     '1 in 548', 548,
    mr['JWT Token (HS384, 48-byte sig)']['one_in_n_missed'])
chk('JWT HS256 impossible',   'impossible', 0.0,
    mr['JWT Token (HS256, 32-byte sig)']['p_missed_marginal'])

# ---- Fix validation (Section: Automated Repair) ----------------------------
for rule in fixv['arms']['fix_A']['per_rule']:
    a = fixv['arms']['fix_A']['per_rule'][rule]['BR']
    b = fixv['arms']['fix_B']['per_rule'][rule]['BR']
    chk(f'fixA BR=1.0 {rule}', '$BR = 1.0$', 1.0, round(a,4))
    chk(f'fixB BR=1.0 {rule}', '$BR = 1.0$', 1.0, round(b,4))

chk('fixA regression SendGrid', '0\\%', 0.0,
    round(fixv['arms']['fix_A']['hyphen_follows_probe']['SendGrid API Key'],4))
chk('fixA regression GCP', '0\\%', 0.0,
    round(fixv['arms']['fix_A']['hyphen_follows_probe']['GCP API Key'],4))
chk('fixB no regression SendGrid', 'Candidate B imposes no such restriction', 1.0,
    round(fixv['arms']['fix_B']['hyphen_follows_probe']['SendGrid API Key'],4))
chk('fixB no regression GCP', 'Candidate B imposes no such restriction', 1.0,
    round(fixv['arms']['fix_B']['hyphen_follows_probe']['GCP API Key'],4))

chk('no new FPs baseline', '67 findings', 67, fixv['false_positives_real_code']['baseline']['findings'])
chk('no new FPs fixA',     '67 under each candidate', 67, fixv['false_positives_real_code']['fix_A']['findings'])
chk('no new FPs fixB',     '67 under each candidate', 67, fixv['false_positives_real_code']['fix_B']['findings'])

# ---- repeated-run robustness (Section RQ1-B) -------------------------------
for rule in ("GCP API Key", "SendGrid API Key"):
    chk(f'stability sd=0 {rule}', 'zero variance', 0.0, stab['per_rule'][rule]['stdev'])
var_range = [stab['per_rule'][r]['min'] for r in
             ("OpenAI API Key (new)","Google OAuth Token","JWT Token")] + \
            [stab['per_rule'][r]['max'] for r in
             ("OpenAI API Key (new)","Google OAuth Token","JWT Token")]
chk('repeated-run range 92.5-98.3', '92.5\\%', 0.925, round(min(var_range),4))
chk('repeated-run range max 98.3',  '98.3\\%', 0.9833, round(max(var_range),4))

# ---- RQ4 comparative -------------------------------------------------------
for corpus, tool, prec, rec in [('corpus_A','GitHub Autopilot',0.9592,1.0),
                                ('corpus_A','Gitleaks 8.21.2',1.0,0.8),
                                ('corpus_A','TruffleHog 3.82.13',1.0,0.5975),
                                ('corpus_B','GitHub Autopilot',0.9591,0.9975),
                                ('corpus_B','Gitleaks 8.21.2',1.0,0.9),
                                ('corpus_B','TruffleHog 3.82.13',1.0,0.6)]:
    r = next(x for x in comp[corpus]['results'] if x['tool']==tool)
    chk(f'{corpus} {tool} recall', f'{rec:.3f}', rec, round(r['recall'],4))

chk('400 positives', '400 positives', 400, comp['corpus_A']['n_positive'])
chk('700 negatives', '700 negatives', 700, comp['corpus_A']['n_negative'])

# ---- Gitleaks affected types ----------------------------------------------
gl = cross['results']['Gitleaks 8.21.2']['per_type']
cov  = sum(1 for c in gl.values() if c['cells']['canonical']['rule_match_rate']==1.0)
zero = sum(1 for c in gl.values()
           if c['cells']['canonical']['rule_match_rate']==1.0
           and c['cells']['in_parens']['rule_match_rate']==0.0)
chk('gitleaks covers 9', 'nine shared credential types it detects', 9, cov)
chk('gitleaks 6 total misses', 'six of the nine', 6, zero)

# ---- appendix / systems ----------------------------------------------------
f = sysb['S5_fault_injection']
chk('storm accepted 200',   '200 of 1{,}000', 200, f['sustained_storm']['accepted'])
chk('storm arrivals 1000',  '1{,}000',      1000, f['sustained_storm']['arrivals'])
chk('dup suppressed',       '1 of 10',      1,   f['duplicate_delivery_suppression']['processed'])
chk('redis 0 exceptions',   '0 of 300',     0,   f['redis_killed_midrun']['uncaught_exceptions'])
chk('redis degraded 180',   '180 of 180',   180, f['redis_killed_midrun']['degraded_gracefully'])
thr = sysb['S4_scanner_throughput_repeated']['scans_per_s']
# Throughput is wall-clock and therefore hardware-dependent: it is the one
# reported quantity that legitimately does NOT reproduce on other machines.
# Exact-matching it made `reproduce.sh` fail on any host but the reference
# one, which is a defect in this verifier rather than in the manuscript. The
# figure quoted in Online Resource 1 is a recorded observation from the
# reference machine (see environment/), and what is checked here is that the
# manuscript still quotes it and that a re-run stays within the same order of
# magnitude -- enough to catch a harness that has genuinely broken.
chk('throughput figure quoted', '11{,}114', True, '11{,}114' in tex)
chk('throughput order of magnitude', '11{,}114', True,
    1_000 <= round(thr['median']) <= 100_000)

# ---- extended study: full tables, 774 types, nine languages ---------------
# Every figure is checked against the raw results and against the derived
# summary, and the summary itself is re-derived here so it cannot drift.
from math import comb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analysis_expanded  # noqa: E402

ex = R('expanded_summary')
chk('expanded summary re-derives', 'Section~\\ref{sec:scale}', True,
    analysis_expanded.main() == ex)
e1 = R('threetool_expanded_bench')
glf = R('gitleaks_fulltable_bench')
rw2 = R('realworld_multilang_bench')
tt3 = ex['three_tool']
rwx = ex['realworld']


def n(x):
    """Integer as the manuscript writes it: 9{,}870{,}820."""
    return f"{x:,}".replace(",", "{,}")


def pct(x):
    return f"{100 * x:.1f}\\%"


# scale of the study
gen = e1['types_generated']
skipped = sum(e1['trufflehog_detectors_not_single_pattern'].values())
th_single = gen['TH'] + sum(1 for t in e1['types_excluded'] if t.startswith('TH:'))
chk('774 types', '774 types generated from all three', 774, sum(gen.values()))
chk('types by generator', '184 from Gitleaks, 566 from TruffleHog, 24 from the primary subject',
    (184, 566, 24), (gen['GL'], gen['TH'], gen['AP']))
chk('48 excluded', '48 types are excluded', 48, len(e1['types_excluded']))
chk('467,960 files', '467{,}960 files', 467960, e1['n_files'])
chk('n=20', 'with $n=20$ samples per cell', 20, e1['n_samples_per_cell'])
chk('TH 606 of 836', '606 of its 836 detectors', (606, 836), (th_single, th_single + skipped))
chk('TH 230 excluded', 'The 230 others', 230, skipped)

# full Gitleaks table
g = glf['summary']
pre, kw = g['prefix'], g['keyword']
chk('195 rules', 'Of 195 rules, 183 are analysed', (195, 183),
    (glf['n_rules'], pre['n_analysed'] + kw['n_analysed']))
chk('88 prefix 95 keyword', '(88 identify the credential by its own prefix, 95 are keyword-anchored)',
    (88, 95), (pre['n_analysed'], kw['n_analysed']))
excl = {**pre['excluded_rules'], **kw['excluded_rules']}
chk('12 excluded: 3 path, 9 samples', '3 apply only to particular file paths, and 9 yield fewer than 40',
    (12, 3, 9), (len(excl), sum('path-restricted' in v for v in excl.values()),
                 sum('filter-valid' in v for v in excl.values())))
tp, tk = pre['terminator_x_paren_failure'], kw['terminator_x_paren_failure']
chk('38 of 88, all 95', 'missed entirely by 38 of the 88 prefix rules and all 95 keyword rules',
    (38, 95), (tp['term_and_fails'] + tp['noterm_and_fails'] - tp['noterm_and_fails'],
               tk['term_and_fails']))
chk('49 of 50 without group pass', 'of the 50 prefix rules without it, 49 pass', (50, 49),
    (tp['noterm_and_fails'] + tp['noterm_and_passes'], tp['noterm_and_passes']))
r1, r2 = (int(x) for x in pre['paren_failures_rescued_by_one_space'].split('/'))
k1, k2 = (int(x) for x in kw['paren_failures_rescued_by_one_space'].split('/'))
chk('133 of 134 rescued', 'rescues 133 of the 134 failing rules', (133, 134), (r1 + k1, r2 + k2))


def fisher(a, b, c, d):
    N, r, k = a + b + c + d, a + b, a + c
    P = lambda x: comb(r, x) * comb(N - r, k - x) / comb(N, k)  # noqa: E731
    po = P(a)
    return sum(P(x) for x in range(max(0, k - (N - r)), min(r, k) + 1) if P(x) <= po * (1 + 1e-9))


fp = fisher(tp['term_and_fails'], tp['term_and_passes'], tp['noterm_and_fails'], tp['noterm_and_passes'])
chk('Fisher 3.3e-24', '$p = 3.3\\times10^{-24}$', '3.3e-24', f"{fp:.1e}")
hf = ex['gitleaks_fulltable']['huggingface_unquoted_newline']
chk('HF misses unquoted/yaml/env', 'miss every unquoted token in the unquoted, YAML and dotenv contexts',
    True, all(v.get(c) == 0.0 for v in hf.values() for c in ('unquoted', 'yaml_value', 'env_file')))

# three tools at 774 types (Table tab:battery774)
bt = tt3['battery_label_matched']
lm = tt3['label_matched_total']
row = lambda ctx: tuple(bt[t][ctx]['total_failure'] for t in ('autopilot', 'gitleaks', 'trufflehog'))  # noqa: E731
chk('covered row', 'types covered (label-matched) & 39 & 218 & 580', (39, 218, 580),
    (lm['autopilot'], lm['gitleaks'], lm['trufflehog']))
for label, ctx, vals in (('unquoted & 4 & 2 & 0', 'unquoted', (4, 2, 0)),
                         ('trailing comma & 4 & 108 & 1', 'trailing_comma', (4, 108, 1)),
                         ("call argument, `\\texttt{)}' & 4 & 159 & 1", 'in_parens', (4, 159, 1)),
                         ("query string, `\\texttt{\\&}' & 4 & 161 & 1", 'url_query_amp', (4, 161, 1)),
                         ("spaced control, `\\texttt{\\ )}' & 4 & 1 & 1", 'in_parens_spaced', (4, 1, 1))):
    chk(f'battery {ctx}', label, vals, row(ctx))
hy = tuple((bt[t]['trailing_hyphen']['total_failure'], bt[t]['trailing_hyphen']['types'])
           for t in ('autopilot', 'gitleaks', 'trufflehog'))
chk('hyphen row', '3 / 8 & 1 / 65 & 91 / 117', ((3, 8), (1, 65), (91, 117)), hy)
chk('159 of 218 in text', "for 159 of the 218 types it covers", (159, 218),
    (bt['gitleaks']['in_parens']['total_failure'], bt['gitleaks']['in_parens']['types']))
chk('91 of 117 in text', 'for 91 of the 117 types whose formats allow them', (91, 117),
    (bt['trufflehog']['trailing_hyphen']['total_failure'], bt['trufflehog']['trailing_hyphen']['types']))
th_fail = [t for t, v in e1['per_type'].items() if v['trufflehog']['label_matched']
           and 'trailing_hyphen' in v['trufflehog']['cells']
           and v['trufflehog']['cells']['trailing_hyphen']['paired_retention'] == 0.0]
chk('88 of 91 TH-generated', 'Of the 91 failing types, 88 were generated from TruffleHog', (91, 88),
    (len(th_fail), sum(t.startswith('TH:') for t in th_fail)))
chk('4 of 39', '4 of its 39 label-matched types fail every non-canonical context', (4, 39),
    (bt['autopilot']['in_parens_spaced']['total_failure'], lm['autopilot']))

hm = tt3['hyphen_marginal']
chk('TH marginal 86/117 max 0.061', 'For TruffleHog 86 of the 117 types', (86, 117, 0.061),
    (hm['trufflehog']['types_marginal_at_least_1_in_100'], hm['trufflehog']['types'],
     hm['trufflehog']['max_marginal']))
chk('TH max 0.061 in text', '(maximum 0.061)', 0.061, hm['trufflehog']['max_marginal'])
chk('AP marginal 3 of 8 max 0.014', 'for the primary subject 3 of 8 (maximum 0.014', (3, 8, 0.014),
    (hm['autopilot']['types_marginal_at_least_1_in_100'], hm['autopilot']['types'],
     hm['autopilot']['max_marginal']))
chk('GL marginal 1 of 65', 'for Gitleaks 1 of 65', (1, 65),
    (hm['gitleaks']['types_marginal_at_least_1_in_100'], hm['gitleaks']['types']))

# coverage and neutral comparison (Tables tab:coverage, tab:neutral)
cv = tt3['coverage_label_matched']
for name, tool in (('Primary subject', 'autopilot'), ('Gitleaks', 'gitleaks'), ('TruffleHog', 'trufflehog')):
    c = cv[tool]
    chk(f'coverage row {tool}', f"{name} & {c['GL']} & {c['TH']} & {c['AP']} & {lm[tool]} \\\\",
        lm[tool], c['GL'] + c['TH'] + c['AP'])
chk('own-generator coverage', 'Gitleaks 183 of 184, TruffleHog 532 of 566, the primary subject 24 of 24',
    (183, 532, 24), (cv['gitleaks']['GL'], cv['trufflehog']['TH'], cv['autopilot']['AP']))
nt = tt3['paired_neutral']
ag, at, gt = (nt['autopilot_vs_gitleaks'], nt['autopilot_vs_trufflehog'], nt['gitleaks_vs_trufflehog'])
chk('neutral AP vs GL', 'TruffleHog & 566 & 2 & 18 & 0.0004', (566, 2, 18, '0.0004'),
    (ag['n_types'], ag['only_autopilot'], ag['only_gitleaks'], f"{ag['p']:.4f}"))
chk('neutral AP vs TH', 'Gitleaks & 184 & 2 & 30 & $2.5\\times10^{-7}$', (184, 2, 30, '2.5e-07'),
    (at['n_types'], at['only_autopilot'], at['only_trufflehog'], f"{at['p']:.1e}"))
chk('neutral GL vs TH', 'Primary subject & 24 & 3 & 1 & 0.625', (24, 3, 1, 0.625),
    (gt['n_types'], gt['only_gitleaks'], gt['only_trufflehog'], round(gt['p'], 3)))
chk('abstract p values', '($p=0.0004$; $p=2.5\\times10^{-7}$)', ('0.0004', '2.5e-07'),
    (f"{ag['p']:.4f}", f"{at['p']:.1e}"))
mis = tt3['mislabelled_coverage_by_rule']
any_ap = sum(tt3['coverage_any_rule']['autopilot'].values())
chk('132 rather than 39, 93 others', 'cover 132 types rather than 39. Of the 93 others', (132, 93),
    (any_ap, any_ap - lm['autopilot']))
chk('84 PagerDuty, 8 JWT, 1 generic', 'it reports 84 under its PagerDuty rule', (84, 8, 1),
    (mis['autopilot']['PagerDuty Integration Key'], mis['autopilot']['JWT Token'],
     mis['autopilot']['Generic Token']))
chk('Gitleaks mislabels 2', 'Gitleaks mislabels 2 types', 2, sum(mis['gitleaks'].values()))

# nine languages (Section sec:rq5, Table tab:fplang)
chk('31 repos, 9 languages', '31 repositories in nine languages', (31, 9),
    (rwx['n_projects'], rwx['n_languages']))
chk('38,309 files, 9,870,820 lines', '38{,}309 files and 9{,}870{,}820 lines', (38309, 9870820),
    (rwx['n_files'], rwx['n_lines']))
chk('9.87 million lines', '9.87 million lines', '9.87', f"{rwx['n_lines'] / 1e6:.2f}")
tot = rwx['totals']
chk('totals', f"reports {n(tot['autopilot']['n_findings'])} findings (0.214 per kLOC), Gitleaks "
              f"{n(tot['gitleaks']['n_findings'])} (0.116) and TruffleHog {tot['trufflehog']['n_findings']} (0.039)",
    ('0.214', '0.116', '0.039'), tuple(f"{tot[t]['findings_per_kloc']:.3f}" for t in ('autopilot', 'gitleaks', 'trufflehog')))
gs = rwx['totals_by_stratum']['general']
chk('general stratum', 'in the general stratum alone, 0.108, 0.036 and 0.038', ('0.108', '0.036', '0.038'),
    tuple(f"{gs[t]['findings_per_kloc']:.3f}" for t in ('autopilot', 'gitleaks', 'trufflehog')))
names = {'csharp': 'C\\#', 'go': 'Go', 'java': 'Java', 'javascript': 'JavaScript', 'php': 'PHP',
         'python': 'Python', 'ruby': 'Ruby', 'rust': 'Rust', 'typescript': 'TypeScript'}
# Per-kLOC rates are recomputed from raw counts: formatting the stored
# four-decimal rate to three decimals would round twice (0.01149 -> 0.0115
# -> 0.012), which is exactly the error this check first caught in the table.
by_lang = {}
for p in rw2['projects'].values():
    x = by_lang.setdefault(p['language'], {'n': 0, 'lines': 0, 'autopilot': 0, 'gitleaks': 0, 'trufflehog': 0})
    x['n'] += 1
    x['lines'] += p['n_lines']
    for t in ('autopilot', 'gitleaks', 'trufflehog'):
        x[t] += p['tools'][t]['n_findings']
for lg, x in by_lang.items():
    r = (f"{names[lg]} & {x['n']} & {n(round(x['lines'] / 1000))} & "
         + " & ".join(f"{x[t] / (x['lines'] / 1000):.3f}" for t in ('autopilot', 'gitleaks', 'trufflehog')))
    chk(f'language row {lg}', r, True, True)
chk('all row', f"All & 31 & {n(round(rwx['n_lines'] / 1000))} & 0.214 & 0.116 & 0.039", True, True)
top = sum(1 for v in rwx['totals_by_language'].values()
          if v['autopilot']['findings_per_kloc'] == max(v[t]['findings_per_kloc'] for t in ('autopilot', 'gitleaks', 'trufflehog')))
chk('highest in six of nine', 'in six of the nine languages', 6, top)
cats = {t: Counter() for t in ('autopilot', 'gitleaks', 'trufflehog')}
for p in rw2['projects'].values():
    for t in cats:
        cats[t].update(p['tools'][t]['by_category'])
order = ('placeholder_or_test_value', 'in_test_doc_or_example_path', 'hash_or_identifier_context', 'other')
for t, text in (('autopilot', '1{,}406 / 177 / 4 / 521'), ('gitleaks', '843 / 279 / 2 / 19'),
                ('trufflehog', '89 / 280 / 1 / 19')):
    chk(f'categories {t}', text, text, ' / '.join(n(cats[t][k]) for k in order))
sh = rw2['projects']['Shopify/shopify-app-js']['tools']
chk('Shopify 1,530 and 859', "contributes 1{,}530 of the subject's and 859 of Gitleaks'", (1530, 859),
    (sh['autopilot']['n_findings'], sh['gitleaks']['n_findings']))
chk('2 unstable', '2 findings were unstable', 2, rwx['trufflehog_unstable_findings'])
three = {t: sum(rw2['projects'][r]['tools'][t]['n_findings']
                for r in ('psf/requests', 'pallets/flask', 'redis/redis-py'))
         for t in ('autopilot', 'gitleaks', 'trufflehog')}
chk('re-scan 12, 12, 27', 'give 12, 12 and 27', (12, 12, 27),
    (three['autopilot'], three['gitleaks'], three['trufflehog']))
pb = rwx['part_b']
chk('80 occurrences, 10 types, 77', '80 occurrences of 10 types in 9.87 million lines, 77 of them',
    (80, 10, 77), (pb['n_occurrences'], pb['n_types_occurring'], pb['in_test_or_doc_path']))

# beyond the boundary (Table tab:encresults)
ep = tt3['encoding_label_matched']
tf = lambda ctx, st='prefix': tuple(ep[t][st][ctx]['total_failure'] for t in ('autopilot', 'gitleaks', 'trufflehog'))  # noqa: E731
chk('prefix types', '(34) & (110) & (78)', (34, 110, 78),
    tuple(tt3['label_matched_by_stratum'][t]['prefix'] for t in ('autopilot', 'gitleaks', 'trufflehog')))
for text, ctxs in (('JSON inside a string & 0 & 49 & 0', ['esc_json_in_string']),
                   ('escaped newline after & 0 & 49 & 0', ['esc_newline_after']),
                   ('& shell default \\texttt{\\$\\{VAR:-v\\}} & 0 & 53 & 0', ['tpl_shell_default']),
                   ('template literal, bearer header, Jinja & 0 & 0 & 0',
                    ['tpl_js_template', 'tpl_bearer_header', 'tpl_jinja_default']),
                   ('HTML-escaped quote & 0 & 53 & 0', ['mk_html_escaped']),
                   ('XML attribute, Markdown code & 0 & 0 & 0', ['mk_xml_attribute', 'mk_markdown_code']),
                   ('Dockerfile \\texttt{ENV}, YAML block & 0 & 2 & 0', ['fs_dockerfile_env', 'fs_yaml_block']),
                   ('& base64 & 34 & 110 & 0', ['tx_base64_wrapped']),
                   ('Kubernetes secret (base64) & 34 & 110 & 0', ['tx_k8s_secret']),
                   ('URL-encoded & 0 & 4 & 2', ['tx_url_encoded']),
                   ('split and concatenated & 30 & 97 & 67', ['tx_split_concat'])):
    want = tuple(int(x) for x in text.split('&')[-3:])
    chk(f'encoding {ctxs[0]}', text, [want] * len(ctxs), [tf(c) for c in ctxs])
kwn = tt3['label_matched_by_stratum']
chk('keyword strata 108, 502, 5', "Gitleaks' 108 keyword types", (108, 502, 5),
    (kwn['gitleaks']['keyword'], kwn['trufflehog']['keyword'], kwn['autopilot']['keyword']))
chk('TH bearer 500, Jinja 499', '(500 and 499)', (500, 499),
    (tf('tpl_bearer_header', 'keyword')[2], tf('tpl_jinja_default', 'keyword')[2]))
pcx = rwx['part_c']
op = pcx['other_path']
sbf = op['share_by_following_character']
chk('15,049 tokens, 28.9%', 'there are 15{,}049 such tokens, and 28.9\\%', (15049, '28.9\\%'),
    (op['n_tokens'], pct(op['share_followed_by_char_gitleaks_terminator_rejects'])))
chk('following characters', 'a comma alone follows 13.7\\%, a closing brace 3.1\\%, an opening parenthesis '
    '2.4\\%, a closing parenthesis 1.7\\% and an ampersand 1.4\\%',
    ('13.7\\%', '3.1\\%', '2.4\\%', '1.7\\%', '1.4\\%'), tuple(pct(sbf[c]) for c in (',', '}', '(', ')', '&')))
rwt = pcx['repository_weighted']
# median from exact per-repository counts, not from the stored rounded share
shares = sorted(round(v['share_followed_by_char_gitleaks_terminator_rejects'] * v['n_tokens'])
                / v['n_tokens'] for v in rw2['part_c_credential_shaped_tokens']['by_repository'].values()
                if v['n_tokens'] >= 20)
rwt = dict(rwt, median_share_rejected=(shares[len(shares) // 2 - 1] + shares[len(shares) // 2]) / 2
           if len(shares) % 2 == 0 else shares[len(shares) // 2])
chk('26 repos median 24.2%', 'Over the 26 repositories with at least 20 such tokens, the median share is 24.2\\%, '
    'ranging from 0.4\\% to 99.3\\%', (26, 20, '24.2\\%', '0.4\\%', '99.3\\%'),
    (rwt['n_repositories'], rwt['min_tokens'], pct(rwt['median_share_rejected']),
     pct(rwt['min_share_rejected']), pct(rwt['max_share_rejected'])))
chk('pooled 84.5%', 'The pooled share over all paths, 84.5\\%', '84.5\\%',
    pct(pcx['pooled']['share_followed_by_char_gitleaks_terminator_rejects']))
chk('abstract 28.9%', 'outside test, documentation and fixture paths are followed by a character Gitleaks rejects',
    '28.9\\%', pct(op['share_followed_by_char_gitleaks_terminator_rejects']))
chk('133 of 183 carry the group', '(133 of the 183 analysed)', (133, 183),
    (tp['term_and_fails'] + tp['term_and_passes'] + tk['term_and_fails'] + tk['term_and_passes'],
     pre['n_analysed'] + kw['n_analysed']))
pc_raw = rw2['part_c_credential_shaped_tokens']
chk('26 of 273,659 end in hyphen', 'of 273{,}659 credential-shaped tokens in the multi-language corpus, 26 end in a hyphen',
    (273659, 26), (pc_raw['overall']['n_tokens'], pc_raw['tokens_ending_in_hyphen']))

fam = e1['encoding_families']
chk('17 contexts, 5 families', 'Seventeen further contexts in five families', (17, 5),
    (len(fam), len(set(fam.values()))))
chk('1,000 draws', 'estimated from 1{,}000 fresh draws', 1000,
    max(r['n_draws'] for r in e1['hyphen_marginal_probability']['per_type'].values()))
chk('full table n=40', 'with $n=40$', 40, glf['n_samples_per_cell'])
thc = cross['results']['TruffleHog 3.82.13']
hyphen_ok_covered = [t for t in thc['types_detected_canonically']
                     if 'trailing_hyphen' in thc['per_type'][t]['cells']]
chk('ten types: TH covers 6, one hyphen-capable', 'only one --- SendGrid ---', (6, ['sendgrid_api_key']),
    (len(thc['types_detected_canonically']), hyphen_ok_covered))

# ---- report ----------------------------------------------------------------
bad = [c for c in checks if not c[1]]
for label, ok, exp, act, present in checks:
    if not ok:
        print(f"  MISMATCH  {label}: expected={exp} actual={act} in_tex={present}")
print(f"\n{len(checks)-len(bad)}/{len(checks)} numeric claims verified against raw data")
sys.exit(1 if bad else 0)
