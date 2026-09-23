// One-page resume. All data arrives as JSON through sys.inputs, so resume text is never
// parsed as Typst markup (a `#` or `$` in a bullet can't break the build).
#let d = json(bytes(sys.inputs.data))
#let st = d.style
#let g(x, k) = x.at(k, default: none)

#set document(title: d.name + " — Resume", author: d.name)
#set page(paper: "us-letter", margin: (x: st.margin_x * 1in, y: st.margin_y * 1in))
#set text(font: "New Computer Modern", size: st.font_size * 1pt, hyphenate: false)
#set par(leading: st.leading * 1em, spacing: 0.5em)
#set list(indent: 0.4em, body-indent: 0.45em, spacing: st.bullet_gap * 1em, marker: [•])

#align(center)[
  #text(size: 2em, weight: "bold", d.name)
  #v(-0.6em)
  #d.contact.map(c => if g(c, "url") != none { link(c.url, c.label) } else { c.label }).join([#h(0.5em)|#h(0.5em)])
]

#let heading-line(title) = {
  v(st.section_gap * 1em)
  block(width: 100%, stroke: (bottom: 0.6pt), inset: (bottom: 2.5pt), below: 0.45em,
    text(size: 1.1em, weight: "bold", tracking: 0.03em, upper(title)))
}

#let entry(e) = {
  let opt(k) = if g(e, k) != none { e.at(k) } else { "" }
  let org = opt("org") + if opt("subline") != "" { " — " + opt("subline") } else { "" }
  if opt("location") == "" {
    // Projects: tech stack inline with the name, one line.
    grid(columns: (1fr, auto), column-gutter: 1em,
      [#text(weight: "bold", e.heading)#if org != "" [ #h(0.15em)|#h(0.15em) #emph(org)]], opt("dates"))
  } else {
    grid(columns: (1fr, auto), column-gutter: 1em, row-gutter: 0.4em,
      text(weight: "bold", e.heading), opt("dates"), emph(opt("org")), emph(opt("location")),
      ..if opt("subline") != "" { (emph(opt("subline")), []) } else { () })
  }
  if e.bullets.len() > 0 {
    v(-0.2em)
    list(..e.bullets)
  }
  v(st.entry_gap * 1em)
}

#if g(d, "summary") != none and d.summary != "" {
  heading-line("Summary")
  d.summary
}

#let skills-block() = if d.skills.len() > 0 {
  heading-line("Technical Skills")
  for s in d.skills [
    #text(weight: "bold", s.category): #s.items.join(", ") \
  ]
}

#heading-line("Education")
#for e in d.education { entry(e) }

#if g(d, "skills_first") == true { skills-block() }

#for s in d.sections {
  heading-line(s.title)
  for e in s.entries { entry(e) }
}

#if g(d, "skills_first") != true { skills-block() }
