"""
Compare the final simulation output.json values against the
IEEE-14 StateVariables reference (Rootnet_FULL_NE_11J12h_SV.xml).
"""
import json
import re
import xml.etree.ElementTree as ET

# ── SV reference ──────────────────────────────────────────────
SV_FILE  = "samples/IEEE_14_feeder_NEPLAN_CIM/Rootnet_FULL_NE_11J12h_SV.xml"
OUT_FILE = "output.json"

tree = ET.parse(SV_FILE)
root = tree.getroot()
NS = {
    'cim': 'http://iec.ch/TC57/2012/CIM-schema-cim16#',
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
}

sv_ref = {}
for sv in root.findall('cim:SvVoltage', NS):
    rid = sv.get('{http://www.w3.org/1999/02/22-rdf-syntax-ns#}ID').replace('_5B20', '')
    v   = float(sv.find('cim:SvVoltage.v',    NS).text)
    ang = float(sv.find('cim:SvVoltage.angle', NS).text)
    sv_ref[rid] = {'v': v, 'ang': ang}

# ── Simulation output ─────────────────────────────────────────
with open(OUT_FILE) as f:
    data = json.load(f)

def last_val(series):
    t = str(max(int(x) for x in series.keys()))
    return series[t]

buses_sim = {}
for key, series in data.items():
    m = re.match(r'V_sub_(bus_\d+_{2,}[\d.]+)_(mag_kv|ang_deg)', key)
    if not m:
        continue
    frag, kind = m.group(1), m.group(2)
    buses_sim.setdefault(frag, {})
    if kind == 'mag_kv':
        buses_sim[frag]['v'] = last_val(series)
    else:
        buses_sim[frag]['ang'] = last_val(series)

# ── Map SV bus name -> sim fragment ──────────────────────────
def sv_to_sim(sv_key):
    """'BUS_1   69.0' -> 'bus_1___69.0'"""
    m = re.match(r'BUS_(\d+)\s+([\d.]+)', sv_key)
    if not m:
        return None
    num = m.group(1)
    kv  = m.group(2)
    pad = '_' * max(0, 4 - len(num))   # pad so total width = 4 chars
    return 'bus_' + num + pad + kv

# ── Print comparison ──────────────────────────────────────────
HDR = (
    f"{'Bus':<20}  {'SV V(kV)':>10}  {'Sim V(kV)':>10}  {'dV(kV)':>8}  {'dV%':>7}"
    f"  ||  {'SV ang(°)':>10}  {'Sim ang(°)':>11}  {'d_ang(°)':>10}"
)
print(HDR)
print('-' * len(HDR))

# Transformer LV voltages keyed by branch number (e.g. 'V_branch_8_LV')
# Map SV bus name to transformer branch key by matching voltage level to LV side.
# BUS_8 18.0 -> V_branch_8_LV (NEPLAN transformer numbering matches bus number)
def sv_bus_to_branch_key(sv_key):
    """'BUS_8   18.0' -> 'V_branch_8_LV'"""
    m = re.match(r'BUS_(\d+)\s+[\d.]+', sv_key)
    if not m:
        return None
    k = f"V_branch_{m.group(1)}_LV"
    return k if k in data else None

for sv_key, ref in sorted(sv_ref.items()):
    sk = sv_to_sim(sv_key)
    if sk and sk in buses_sim:
        sv_v = ref['v']
        si_v = buses_sim[sk].get('v', float('nan'))
        sv_a = ref['ang']
        si_a = buses_sim[sk].get('ang', float('nan'))
        dv   = si_v - sv_v
        dvp  = dv / sv_v * 100 if sv_v else float('nan')
        da   = si_a - sv_a
        print(
            f"  {sv_key:<20}  {sv_v:>10.4f}  {si_v:>10.4f}  {dv:>+8.4f}  {dvp:>+6.3f}%"
            f"  ||  {sv_a:>10.4f}  {si_a:>11.4f}  {da:>+10.4f}"
        )
    else:
        # Try transformer LV branch output (e.g. BUS_8 18.0)
        bk = sv_bus_to_branch_key(sv_key)
        if bk:
            sv_v = ref['v']
            si_v = last_val(data[bk])
            dv   = si_v - sv_v
            dvp  = dv / sv_v * 100 if sv_v else float('nan')
            sv_a = ref['ang']
            print(
                f"  {sv_key:<20}  {sv_v:>10.4f}  {si_v:>10.4f}  {dv:>+8.4f}  {dvp:>+6.3f}%"
                f"  ||  {sv_a:>10.4f}  {'(TR LV out)':>11}  {'N/A':>10}"
                f"  [from {bk}]"
            )
        else:
            print(f"  {sv_key:<20}  -- not found in simulation output --")
