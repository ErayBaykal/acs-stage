"""Reconstruct the EtherCAT unit -> axis -> current-rating map from the MMI log.

Each detected unit appears as a discrete "IdentifyUnit ... Finished successfully
with Result:" block, so split on that marker rather than guessing by proximity.
"""
import io, re, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

LOG = r"C:\Users\eryby\AppData\Roaming\ACS Motion Control\SPiiPlus MMI Application Studio\4.20.01.00\Logs\SPiiPlus_MMI_Log_2026-08-26.log"
MARKER = 'Finished successfully with Result: [ACS-SN-SLT'

with open(LOG, 'r', encoding='utf-8', errors='replace') as f:
    lines = f.read().splitlines()

starts = [i for i, l in enumerate(lines) if MARKER in l]

units = {}
for n, s in enumerate(starts):
    end = starts[n + 1] if n + 1 < len(starts) else len(lines)
    block = lines[s:end]
    kv = {}
    for l in block:
        m = re.match(r'^([A-Za-z_0-9]+)=(.*)$', l.strip())
        if m and m.group(1) not in kv:      # first occurrence wins
            kv[m.group(1)] = m.group(2)
    sn = kv.get('OrderingPartNumber') and kv.get('SN')
    key = (kv.get('DIP'), kv.get('NetworkAxes0'))
    if kv.get('NetworkAxes0') is not None:
        units[key] = kv

STAGES = {'0': 'linear', '1': 'linear', '4': 'rotation', '5': 'rotation', '6': 'rotation'}

print(f"{'DIP':<4} {'Axes':<7} {'PartNumber':<15} {'Serial':<10} "
      f"{'Cont/Peak':<10} {'Vdc':<4} {'SinCos':<7} {'Abs':<4} Stages")
print('-' * 88)
for (dip, axes) in sorted(units, key=lambda k: int(k[0])):
    kv = units[(dip, axes)]
    used = [f"{a}:{STAGES[a]}" for a in axes.split(',') if a in STAGES]
    print(f"{dip:<4} {axes:<7} {kv.get('OrderingPartNumber',''):<15} "
          f"{kv.get('SN',''):<10} "
          f"{kv.get('Nominal','?')+'/'+kv.get('Peak','?'):<10} "
          f"{kv.get('Voltage','?'):<4} "
          f"{kv.get('N_ALLOWED_SIN_COS_ENCODERS_250kHz_0','?'):<7} "
          f"{kv.get('N_ALLOWED_ABS_ENCODERS_0','?'):<4} "
          f"{', '.join(used) if used else '-- unused --'}")
