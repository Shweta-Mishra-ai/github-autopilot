"""
Verify every headline number asserted in the manuscript against the raw
result files. Any mismatch is a defect in the paper, not in the data.
"""
import json, os, re, sys

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
chk('5 fragile rules',      'Five rules are affected', 5, mut['n_boundary_fragile_patterns'])
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
                                ('corpus_A','TruffleHog 3.82.13',1.0,0.6),
                                ('corpus_B','Gitleaks 8.21.2',1.0,0.9)]:
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
chk('gitleaks covers 9', 'nine credential types it detects', 9, cov)
chk('gitleaks 6 total misses', 'six of the nine', 6, zero)

# ---- appendix / systems ----------------------------------------------------
f = sysb['S5_fault_injection']
chk('storm accepted 200',   '200 of 1{,}000', 200, f['sustained_storm']['accepted'])
chk('storm arrivals 1000',  '1{,}000',      1000, f['sustained_storm']['arrivals'])
chk('dup suppressed',       '1 of 10',      1,   f['duplicate_delivery_suppression']['processed'])
chk('redis 0 exceptions',   '0 of 300',     0,   f['redis_killed_midrun']['uncaught_exceptions'])
chk('redis degraded 180',   '180 of 180',   180, f['redis_killed_midrun']['degraded_gracefully'])
thr = sysb['S4_scanner_throughput_repeated']['scans_per_s']
chk('throughput median',    '11{,}114',     11114, round(thr['median']))

# ---- report ----------------------------------------------------------------
bad = [c for c in checks if not c[1]]
for label, ok, exp, act, present in checks:
    if not ok:
        print(f"  MISMATCH  {label}: expected={exp} actual={act} in_tex={present}")
print(f"\n{len(checks)-len(bad)}/{len(checks)} numeric claims verified against raw data")
sys.exit(1 if bad else 0)
