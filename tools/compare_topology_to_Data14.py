#!/usr/bin/env python3
"""Export topology parameters and compare to Data14.m (if present).

Outputs:
  - tools/line_transformer_param_compare.csv  (all topology entries + optional Data14 comparison)
  - summary printed to stdout
"""
import json
import os
import re
import csv

WORKDIR = os.path.dirname(os.path.dirname(__file__)) if __file__ else '.'
TOPO_FILE = os.path.join(WORKDIR, 'topology_export.json')
OUT_CSV = os.path.join(WORKDIR, 'tools', 'line_transformer_param_compare.csv')
S_BASE_MVA = 100.0


def find_data14_file(root='.'):
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower() == 'data14.m' or fn.lower().startswith('data14') and fn.lower().endswith('.m'):
                return os.path.join(dirpath, fn)
    return None


def extract_linedata_from_matlab(path):
    """Attempt to extract a MATLAB `linedata = [ ... ];` matrix from a .m file.
    Returns a list of rows (list of floats) or None if not found.
    """
    txt = open(path, 'r', encoding='utf-8', errors='ignore').read()
    lower = txt.lower()
    idx = lower.find('linedata')
    if idx < 0:
        return None
    # find first '[' after that
    b = lower.find('[', idx)
    if b < 0:
        return None
    # find matching ']' (not robust for nested, but adequate)
    depth = 0
    end = None
    for i in range(b, len(txt)):
        ch = txt[i]
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return None
    block = txt[b+1:end]
    # split rows by semicolon or newline
    rows = re.split(r';|\n', block)
    out = []
    for r in rows:
        # strip comments (MATLAB %)
        r_clean = re.sub(r'%.*', '', r).strip()
        if not r_clean:
            continue
        # split by whitespace, commas
        parts = re.split(r'[\s,]+', r_clean)
        nums = []
        ok = True
        for p in parts:
            if p.strip() == '':
                continue
            try:
                nums.append(float(p))
            except Exception:
                ok = False
                break
        if ok and nums:
            out.append(nums)
    return out


def build_bus_number_map(nodes):
    # map node id/name to bus number if label contains BUS_<num>
    map_name_to_num = {}
    map_num_to_name = {}
    for n in nodes:
        props = n.get('properties', {})
        name = props.get('name') or n.get('label') or props.get('rdf_id')
        if not name:
            continue
        m = re.search(r'BUS[_\s]*(\d+)', name.upper())
        if m:
            num = int(m.group(1))
            map_name_to_num[name] = num
            map_num_to_name[num] = name
    return map_name_to_num, map_num_to_name


def main():
    if not os.path.exists(TOPO_FILE):
        print('Topology export not found:', TOPO_FILE)
        return 1

    topo = json.load(open(TOPO_FILE))
    nodes = topo.get('nodes', [])
    edges = topo.get('edges', [])

    id2name = {}
    for n in nodes:
        props = n.get('properties', {})
        name = props.get('name') or n.get('label') or props.get('rdf_id')
        id2name[n['id']] = name

    bus_name_to_num, num_to_bus_name = build_bus_number_map(nodes)

    lines = []
    for e in edges:
        if e.get('type') != 'LINE':
            continue
        props = e.get('properties', {})
        name = props.get('name')
        fromn = id2name.get(e['from'])
        ton = id2name.get(e['to'])
        vbase = props.get('nominal_voltage_kv')
        r = props.get('r_ohm')
        x = props.get('x_ohm')
        bch = props.get('bch') or 0.0
        if r is None or x is None or vbase is None:
            continue
        r_pu = r * S_BASE_MVA / (vbase**2)
        x_pu = x * S_BASE_MVA / (vbase**2)
        b_total_pu = bch * (vbase**2) / S_BASE_MVA
        b_half_pu = b_total_pu / 2.0
        lines.append({
            'name': name,
            'from': fromn,
            'to': ton,
            'vbase_kv': vbase,
            'r_ohm': r,
            'x_ohm': x,
            'bch_s': bch,
            'r_pu': r_pu,
            'x_pu': x_pu,
            'b_total_pu': b_total_pu,
            'b_half_pu': b_half_pu,
        })

    # Transformers info
    transformers = []
    for n in nodes:
        if n.get('type') != 'Transformer':
            continue
        p = n.get('properties', {})
        name = p.get('name')
        hv_v = p.get('hv_nominal_voltage_kv') or p.get('hv_rated_u_kv')
        lv_v = p.get('lv_nominal_voltage_kv') or p.get('lv_rated_u_kv')
        hv_r = p.get('hv_r_ohm', 0.0)
        hv_x = p.get('hv_x_ohm', 0.0)
        lv_r = p.get('lv_r_ohm', 0.0)
        lv_x = p.get('lv_x_ohm', 0.0)
        # compute pu values referred to hv base (if hv_v present)
        tr = {
            'name': name,
            'hv_v_kv': hv_v,
            'lv_v_kv': lv_v,
            'hv_r_ohm': hv_r,
            'hv_x_ohm': hv_x,
            'lv_r_ohm': lv_r,
            'lv_x_ohm': lv_x,
        }
        if hv_v and (hv_r or hv_x):
            tr['hv_r_pu'] = hv_r * S_BASE_MVA / (hv_v**2)
            tr['hv_x_pu'] = hv_x * S_BASE_MVA / (hv_v**2)
        transformers.append(tr)

    # Attempt to locate Data14.m
    data14_file = find_data14_file(WORKDIR)
    data14_rows = None
    if data14_file:
        print('Found Data14 file:', data14_file)
        data14_rows = extract_linedata_from_matlab(data14_file)
        if not data14_rows:
            print('Could not parse linedata matrix from', data14_file)
    else:
        print('No Data14.m found in workspace; will only export topology params.')

    # Build mapping from bus name <-> bus number for matching
    def match_line_to_data(row):
        # row likely: fbus tbus r x b [tap ...]
        if len(row) < 5:
            return None
        fb = int(row[0]); tb = int(row[1])
        r_pu = float(row[2]); x_pu = float(row[3]); b_pu = float(row[4])
        tap = None
        if len(row) >= 6:
            tap = float(row[5])
        fname = num_to_bus_name.get(fb)
        tname = num_to_bus_name.get(tb)
        if not fname or not tname:
            return None
        # find matching topo line
        for l in lines:
            a = l['from']; b = l['to']
            if (a == fname and b == tname) or (a == tname and b == fname):
                return l, {'fbus': fb, 'tbus': tb, 'r_pu': r_pu, 'x_pu': x_pu, 'b_pu': b_pu, 'tap': tap}
        return None

    # Prepare CSV headers
    headers = [
        'name','from','to','vbase_kv','r_ohm','x_ohm','bch_s','r_pu_topo','x_pu_topo','b_total_pu_topo','b_half_pu_topo',
        'data_r_pu','data_x_pu','data_b_pu','data_tap','delta_r_pu','delta_x_pu','delta_b_pu','match_note'
    ]

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, 'w', newline='') as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=headers)
        writer.writeheader()
        matched = 0
        unmatched = 0
        max_dr = 0.0
        max_dx = 0.0
        max_db = 0.0

        data_rows_map = {}
        if data14_rows:
            for row in data14_rows:
                key = (int(row[0]), int(row[1]))
                data_rows_map[key] = row

        for l in lines:
            out = {h: '' for h in headers}
            out['name'] = l['name']
            out['from'] = l['from']
            out['to'] = l['to']
            out['vbase_kv'] = l['vbase_kv']
            out['r_ohm'] = l['r_ohm']
            out['x_ohm'] = l['x_ohm']
            out['bch_s'] = l['bch_s']
            out['r_pu_topo'] = l['r_pu']
            out['x_pu_topo'] = l['x_pu']
            out['b_total_pu_topo'] = l['b_total_pu']
            out['b_half_pu_topo'] = l['b_half_pu']

            matched_row = None
            if data14_rows:
                # try match by bus numbers (both orders)
                # find bus numbers for from/to
                fb = bus_name_to_num.get(l['from'])
                tb = bus_name_to_num.get(l['to'])
                if fb and tb:
                    dr = data_rows_map.get((fb,tb)) or data_rows_map.get((tb,fb))
                    if dr:
                        matched_row = dr
            if matched_row:
                data_r = float(matched_row[2]); data_x = float(matched_row[3]); data_b = float(matched_row[4])
                out['data_r_pu'] = data_r
                out['data_x_pu'] = data_x
                out['data_b_pu'] = data_b
                out['data_tap'] = matched_row[5] if len(matched_row) >= 6 else ''
                out['delta_r_pu'] = l['r_pu'] - data_r
                out['delta_x_pu'] = l['x_pu'] - data_x
                # decide whether data_b represents B_half or B_total by comparing
                db_half = l['b_half_pu'] - data_b
                db_total = l['b_total_pu'] - data_b
                # choose smaller
                if abs(db_half) <= abs(db_total):
                    out['delta_b_pu'] = db_half
                else:
                    out['delta_b_pu'] = db_total
                out['match_note'] = 'matched'
                matched += 1
                max_dr = max(max_dr, abs(out['delta_r_pu']))
                max_dx = max(max_dx, abs(out['delta_x_pu']))
                max_db = max(max_db, abs(out['delta_b_pu']))
            else:
                out['match_note'] = 'no_data14_match'
                unmatched += 1

            writer.writerow(out)

    # Print summary
    print('\nWrote CSV:', OUT_CSV)
    if data14_rows:
        total = matched + unmatched
        print(f'Compared {matched} lines; {unmatched} unmatched (total topology lines {total}).')
        print(f'Max |ΔR_pu| = {max_dr:.6g}, Max |ΔX_pu| = {max_dx:.6g}, Max |ΔB_pu| = {max_db:.6g}')
    else:
        print('No Data14 comparison performed (Data14.m not found).')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
