---
domain: legal
topic: trade_secrets
company: Waymo/Uber
year: 2017
failure_type: [trade_secret_theft, acquisition_due_diligence, executive_misconduct, ip_contamination]
---

# Waymo v. Uber — Trade Secret Theft (2017–2018)

## Situation

Waymo (Alphabet's self-driving car division, spun out from Google's Project Chauffeur) had spent seven years and hundreds of millions of dollars developing LiDAR sensor technology — a core component of its autonomous vehicle stack. Anthony Levandowski, one of the lead engineers on the project, left Google in January 2016 and founded Otto, a self-driving truck startup. In August 2016, Uber acquired Otto for approximately $680 million and appointed Levandowski to lead Uber's entire self-driving division. Uber was in a high-stakes race with Waymo and others to commercialize autonomous vehicles and viewed the acquisition as a way to close a significant technology gap.

## What Happened

In February 2017, Waymo sued Uber alleging that Levandowski had downloaded approximately 14,000 confidential files — including detailed LiDAR circuit board designs — from Google's servers before his departure. Waymo argued that Uber acquired Otto knowing it was getting access to Waymo's trade secrets. During discovery, a forensic report (the "Stroz Report") commissioned by Uber's own lawyers before the acquisition confirmed that Levandowski had retained Google proprietary data. The trial began in February 2018 but settled five days in: Uber paid Waymo 0.34% of its equity, valued at approximately $245 million. Uber fired Levandowski in May 2017. In 2020, Levandowski pleaded guilty to one federal count of trade secret theft, was sentenced to 18 months in prison and ordered to pay over $756,000 in restitution. He received a presidential pardon in January 2021.

## Root Cause

Uber's leadership treated the acquisition of talent with competitor knowledge as a competitive shortcut and did not enforce the boundaries required to prevent IP contamination. Uber's own pre-acquisition due diligence flagged that Levandowski possessed Google files, but the acquisition proceeded. Once inside Uber, no technical firewall prevented Levandowski's prior knowledge from influencing Uber's LiDAR designs. The organizational incentive — close the technology gap fast — overrode the legal risk that the due diligence had already surfaced.

## Key Decision Failures

- **Due diligence findings ignored**: Uber's pre-acquisition forensic report identified that Levandowski retained Google proprietary data; the acquisition proceeded without resolving the contamination risk or requiring Levandowski to certify destruction
- **No clean-room protocol for acquired engineers**: Uber placed Levandowski directly in charge of its self-driving program with full access to engineering decisions, rather than isolating his contributions behind a clean-room wall that would have protected Uber from IP contamination claims
- **Speed-to-market prioritized over legal exposure**: The $680 million acquisition was driven by competitive urgency; legal risk was treated as a cost of doing business rather than a structural threat to the autonomous vehicle program
- **Executive accountability deferred until litigation forced it**: Levandowski was not removed from his leadership position until months after the lawsuit was filed, despite Uber's own lawyers having prior knowledge of the file downloads

## Lessons

1. **Pre-acquisition due diligence that surfaces risk is only useful if it changes the deal terms**: A forensic report that confirms IP contamination and does not result in contractual protections, escrow, or a deal redesign is evidence the acquirer chose to accept the risk — which becomes evidence against the acquirer in litigation.
2. **Clean-room engineering is the only defense against trade secret contamination claims**: When hiring or acquiring engineers from a direct competitor, the receiving company must isolate the acquired person's contributions from the relevant product area. Without a documented clean-room process, every design similarity becomes circumstantial evidence of misappropriation.
3. **Competitive urgency is not a legal defense**: "We needed to move fast" does not reduce liability for trade secret misappropriation; it increases it, because it establishes motive.
4. **Trade secret litigation can be more expensive than building the technology in-house**: Uber's total cost — the $245 million settlement, the $680 million acquisition that produced no usable IP, the multi-year engineering delay, and the reputational damage — far exceeded what a clean internal R&D program would have cost.
