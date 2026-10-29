"""Render matched-tool review configurations from independently audited results.

Requires matplotlib. Run this file directly to produce PNG, SVG, PDF and data.
"""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

HERE = Path(__file__).resolve().parent
rows = {r['case']: r for r in json.loads((HERE.parent / 'comparison.json').read_text())['runs']}
configs = [
    ('B-feature-single', 'OpenAI · Sol × 1', 'Direct review'),
    ('G-feature-two-codex', 'OpenAI · Sol × 2', 'Synthesis: OpenAI · Sol'),
    ('C-feature-synthesised', 'Anthropic · Opus + OpenAI · Sol', 'Synthesis: OpenAI · Sol'),
    ('H-feature-two-claude', 'Anthropic · Opus × 2', 'Synthesis: Anthropic · Opus'),
    ('J-feature-haiku-terra', 'Anthropic · Haiku + OpenAI · Terra', 'Synthesis: OpenAI · Terra'),
]
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11, 'svg.fonttype': 'none', 'pdf.fonttype': 42})
fig = plt.figure(figsize=(15, 10.5), facecolor='white')
ax = fig.add_axes([.355, .325, .58, .465])
blue, teal, gray = '#245C92', '#2A937F', '#E2E5E9'
fig.text(.05, .948, 'AI code review: useful findings by model setup', fontsize=24, weight='bold', color='#172B40')
fig.text(.05, .91, 'One pull request · five configurations · findings independently checked', fontsize=13, color='#4F5E6B')
legend = [Patch(facecolor=blue, label='Supported bugs'), Patch(facecolor=teal, label='Supported observations'),
          Patch(facecolor=gray, edgecolor='#8A939D', hatch='///', label='Not established')]
fig.legend(handles=legend, loc='upper left', bbox_to_anchor=(.045, .878), ncol=3, frameon=False, handlelength=1.6, columnspacing=2.5)
metadata = []
maximum = max(rows[case]['retained_findings'] for case, _, _ in configs)
for y, (case, title, subtitle) in enumerate(configs):
    r = rows[case]
    bugs = r['audited_behavioral_findings']
    other = r['audited_nonblocking_observations']
    supported = bugs + other
    unestablished = r['retained_findings'] - supported
    assert unestablished >= 0
    ax.barh(y, bugs, height=.46, color=blue)
    if other:
        ax.barh(y, other, left=bugs, height=.46, color=teal)
    if unestablished:
        ax.barh(y, unestablished, left=supported, height=.46, color=gray, edgecolor='#8A939D', hatch='///', linewidth=.6)
    for start, width in ((0, bugs), (bugs, other)):
        if width:
            ax.text(start + width / 2, y, str(width), ha='center', va='center', color='white', weight='bold', fontsize=13)
    if unestablished:
        ax.text(supported + unestablished / 2, y, str(unestablished), ha='center', va='center', color='#46515D', weight='bold', fontsize=13)
    ax.text(max(supported, .5), y - .34, f'{supported} supported', ha='right', va='center', color='#172B40', fontsize=11, weight='bold')
    ax.text(-.04, y - .08, title, transform=ax.get_yaxis_transform(), ha='right', va='center', fontsize=11.5, weight='bold', color='#172B40')
    ax.text(-.04, y + .18, subtitle, transform=ax.get_yaxis_transform(), ha='right', va='center', fontsize=10, color='#586773')
    metadata.append(dict(case=case,review_configuration=title,synthesis=subtitle,supported_bugs=bugs,supported_observations=other,not_established=unestablished,retained_comments=r['retained_findings']))
ax.set_xlim(0, maximum + .5)
ax.set_ylim(len(configs) - .4, -.65)
ax.set_xticks(range(0, maximum + 1, 2))
ax.set_yticks([])
ax.set_xlabel('Distinct findings within each review', color='#4F5E6B', labelpad=12)
ax.tick_params(axis='x', length=0, colors='#586773')
ax.grid(axis='x', color='#E9EDF1', linewidth=.8)
ax.set_axisbelow(True)
for spine in ax.spines.values(): spine.set_visible(False)
fig.text(.05, .255, 'How to read the categories', fontsize=13, weight='bold', color='#172B40')
definitions = [
    ('Bug', 'Demonstrated incorrect behavior or a broken intended workflow.', blue),
    ('Observation', 'Supported design or efficiency concern, without an established failure.', teal),
    ('Not established', 'Insufficient evidence, an unmeasured claim, or a refuted finding.', '#586773'),
]
for y, (label, meaning, color) in zip((.221, .190, .159), definitions):
    fig.text(.05, y, label, fontsize=11, weight='bold', color=color)
    fig.text(.175, y, meaning, fontsize=11, color='#4F5E6B')
fig.text(.05, .112, 'Models: OpenAI GPT-6.1 Sol / Terra via Codex; Anthropic Claude Opus 5.5 / Haiku 5.5. All at high effort.', fontsize=10, color='#586773')
fig.text(.05, .081, 'Same source snapshot and tool revision; one run per setup. Same-model pairs reuse one independent review. Counts overlap across setups.', fontsize=9.6, color='#586773')
fig.text(.05, .050, 'Synthesis combines and checks reviewer reports. Its model also varies here. This is a small case study, not a general model ranking.', fontsize=9.8, color='#586773')
for extension in ('png', 'svg', 'pdf'):
    fig.savefig(HERE / f'review-findings.{extension}', dpi=180, facecolor=fig.get_facecolor())
(HERE / 'review-findings.data.json').write_text(json.dumps(dict(source='../comparison.json',selection='All five configurations use the same feature tool revision. Other revision runs and suggestion continuations are excluded to avoid mixing implementations or counting the same review twice.',category_definitions={k:v for k,v,_ in definitions},rows=metadata),indent=2)+'\n')
print('Saved PNG, SVG, PDF and chart data in', HERE)
