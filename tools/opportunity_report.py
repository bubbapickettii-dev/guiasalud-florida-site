"""Renders config/opportunities.json -> docs/OPPORTUNITIES.md (every field, ranked). Re-run after editing the JSON."""
import json
from pathlib import Path

R = Path(__file__).resolve().parents[1]
d = json.loads((R / "config" / "opportunities.json").read_text())
O = d["opportunities"]
L = [f"# Opportunity database (researched {d['as_of']})", "",
     "Status ladder: HYPOTHESIS → RESEARCHED → READY TO TEST → TESTING → PROVEN / DISPROVEN. Research can only get an opportunity to READY TO TEST; only real consumer behavior moves it further. "
     "Scores are research judgments (1-5 per criterion), not evidence. Weights: " + ", ".join(f"{k.replace('_', ' ')} ×{v}" for k, v in d["weights"].items()) + ".", "",
     "| Rank | ID | Opportunity | Score | Status | Intent | Phone | Competition (5 = low) | Speed | Free leverage |", "|---|---|---|---|---|---|---|---|---|---|"]
for i, o in enumerate(O, 1):
    s = o["scores"]
    L.append(f"| {i} | {o['id']} | {o['title']}{' (hidden)' if o['hidden'] else ''} | **{o['score']}** | {o['status']} | {s['intent']} | {s['phone_potential']} | {s['competition']} | {s['speed_to_test']} | {s['free_leverage']} |")
for sect, hid in (("Researched opportunities", False), ("Hidden / non-obvious opportunities", True)):
    L += ["", f"## {sect}", ""]
    for o in [x for x in O if x["hidden"] == hid]:
        L += [f"### {o['id']} · {o['title']} (score {o['score']}, {o['status']})", ""]
        for k, lbl in [("who", "WHO"), ("why_now", "WHY NOW"), ("trigger", "TRIGGER"), ("geography", "GEOGRAPHY"), ("language", "LANGUAGE"),
                       ("search_behavior", "WHAT THEY SEARCH"), ("community", "WHERE THEY ARE / WHAT THEY DISCUSS"), ("who_reaches_them", "WHO ALREADY REACHES THEM"),
                       ("distribution", "HOW WE REACH THEM"), ("content", "WHAT WE PUT IN FRONT OF THEM"), ("offer", "OFFER"), ("cta", "CTA"), ("check_angle", "CHECK ANGLE"),
                       ("why_click", "WHY THEY'D CLICK"), ("why_check", "WHY THEY'D COMPLETE THE CHECK"), ("why_phone", "WHY THEY'D GIVE A PHONE"),
                       ("lead_quality", "EXPECTED LEAD QUALITY"), ("est_cost", "ESTIMATED COST"), ("competition", "COMPETITION"), ("scalability", "SCALABILITY"),
                       ("difficulty", "DIFFICULTY"), ("different", "DIFFERENT FROM THE NORMAL PLAYBOOK"), ("test", "TEST RECOMMENDATION"), ("decision", "DECISION"), ("result", "RESULT")]:
            if o.get(k):
                L.append(f"- **{lbl}:** {o[k]}")
        L.append("- **EVIDENCE:**")
        for e in o["evidence"]:
            L.append(f"  - {e[2]}: {e[0]}" + (f" ([source]({e[1]}))" if e[1] != "-" else ""))
        L.append("- **SCORES:** " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in o["scores"].items()))
        L.append(f"- **EXPERIMENTS:** {', '.join(o['experiments']) or 'none yet'}")
        L.append("")
(R / "docs" / "OPPORTUNITIES.md").write_text("\n".join(L) + "\n")
print("docs/OPPORTUNITIES.md", len(O), "opportunities")
