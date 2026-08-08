#import "@preview/cetz:0.3.4": canvas, draw

#let similarity-chart = canvas(length: 0.9cm, {
  // (label, group, value, display-string)
  let data = (
    ("B9 ∠",        "Voltage", 0.7885, "0.79%"),
    ("B10 ∠",       "Voltage", 0.7271, "0.73%"),
    ("B7 ∠",        "Voltage", 0.7166, "0.72%"),
    ("Bat P",        "Battery", 0.4197, "0.42%"),
    ("Bat I",        "Battery", 0.4054, "0.41%"),
    ("Bat SOC",      "Battery", 0.0084, "0.01%"),
    ("PV P(W)",      "PV",      0.0000, "0.00%"),
    ("PV P",         "PV",      0.0000, "0.00%"),
    ("PV I",         "PV",      0.0000, "0.00%"),
    ("Q loss tr.",   "Losses",  1.6364, "1.64%"),
    ("Q loss total", "Losses",  0.8975, "0.90%"),
    ("Q loss line",  "Losses",  0.7677, "0.77%"),
  )

  let clr = (
    "Voltage": rgb("#1f77b4"),
    "Battery": rgb("#ff7f0e"),
    "PV":      rgb("#2ca02c"),
    "Losses":  rgb("#9467bd"),
  )

  let ax  = 2.2   // y-axis x
  let bw  = 0.85  // bar width
  let bsp = 1.35  // spacing within group
  let gsp = 1.8   // extra gap between groups
  let ys  = 6.5   // canvas units per 1%

  // compute bar centres
  let cx = ()
  let x = ax + 1.1
  for (i, entry) in data.enumerate() {
    cx = cx + (x,)
    let same-next = (i + 1 < data.len()) and (data.at(i + 1).at(1) == entry.at(1))
    x = x + bsp + (if same-next { 0.0 } else { gsp })
  }
  let xend = x + 0.2

  // gridlines
  for yt in (0.0, 0.5, 1.0, 1.5) {
    let yc = yt * ys
    draw.line((ax, yc), (xend, yc),
      stroke: (paint: luma(195), thickness: 0.35pt, dash: "dotted"))
    draw.content((ax - 0.15, yc),
      align(right, text(size: 8pt, fill: black)[#(str(yt) + "%")]),
      anchor: "east")
  }

  // bars + value labels + x labels
  for (i, (lbl, grp, val, disp)) in data.enumerate() {
    let xc   = cx.at(i)
    let ybar = val * ys

    draw.rect((xc - bw/2, 0), (xc + bw/2, ybar),
      fill: clr.at(grp), stroke: none)

    draw.content((xc, ybar + 0.18),
      text(size: 7.5pt, weight: "medium", fill: black)[#disp],
      anchor: "south")

    draw.content((xc, -0.2),
      rotate(-45deg, text(size: 8pt, fill: black)[#lbl]),
      anchor: "north-east")
  }

  // group label bands below x-axis
  let groups = (
    ("Voltage", 0,  2),
    ("Battery", 3,  5),
    ("PV",      6,  8),
    ("Losses",  9, 11),
  )
  for (grp, i0, i1) in groups {
    let x0 = cx.at(i0) - bw/2 - 0.15
    let x1 = cx.at(i1) + bw/2 + 0.15
    draw.rect((x0, -1.55), (x1, -1.15),
      fill: clr.at(grp).transparentize(75%), stroke: none)
    draw.content(((x0 + x1) / 2, -1.35),
      text(size: 8pt, weight: "medium", fill: clr.at(grp))[#grp],
      anchor: "center")
  }

  // axes
  draw.line((ax, 0), (ax, 1.75 * ys + 0.4),
    stroke: (paint: black, thickness: 0.7pt),
    mark: (end: "straight", size: 0.15))
  draw.line((ax, 0), (xend, 0),
    stroke: (paint: black, thickness: 0.7pt))

  // y-axis label
  draw.content((ax - 1.7, ys),
    rotate(90deg, text(size: 9pt, weight: "medium", fill: black)[Relative MAE (%)]),
    anchor: "center")
})
