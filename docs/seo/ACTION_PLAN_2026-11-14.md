# SEO action plan (from the 14 November 2026 audit)

"Code" means it can be changed in the frontend repo. "You" means it needs a fact or an account only the owner has.

## Phase 1: Critical (this week)

| # | Fix | Who | Effort |
|---|---|---|---|
| 1 | Make pricing identical everywhere: replace the homepage FAQ "credits" answer and its FAQ markup with the rate card; remove or correct "No monthly fee" | Code | 1-2 h |
| 2 | Fix the phone-number wording on the homepage hero and FAQ; correct /docs wording for the "buy a number" endpoint | Code | 1 h |
| 3 | Rewrite /privacy: legal entity, data collected, purposes, retention, sub-processors, grievance officer and contact, rights under India's DPDP Act | You (facts) + code; lawyer review | half a day |
| 4 | Add company facts to /about and the footer: legal name, registered address, contact | You (facts) + code | 1 h |

## Phase 2: High (within 2 weeks)

| # | Fix | Who | Effort |
|---|---|---|---|
| 5 | Verify the site in Bing Webmaster Tools and Google Search Console, submit the sitemap, add the Bing verification tag, submit URLs via IndexNow (key file already live) | You (accounts) + code | 1-2 h |
| 6 | Schema: remove HowTo; add WebPage/AboutPage/ContactPage + BreadcrumbList + Offers to /pricing and the other static pages; reference `#organization` by @id from answer pages; add Autobot360 as parent, PNG logo, real profile links | Code | half a day |
| 7 | Mobile: add a menu to the homepage header, make the pricing table scroll or stack, raise tap targets to 44 px | Code | half a day |
| 8 | Resolve contradictions: language count (English + 10 spoken), own-key rule wording, "never makes things up" | Code | 1 h |
| 9 | Hindi page: set `lang="hi-IN"`, translate its Related block | Code | 30 min |
| 10 | Link /pricing in the homepage header; link more answer pages from the homepage; link /docs from the footer and answers | Code | 1 h |

## Phase 3: Medium (within a month)

| # | Fix | Who | Effort |
|---|---|---|---|
| 11 | Re-encode the hero video to 2-3 MB desktop and about 1 MB mobile with a mobile source; do not preload; skip on data-saver and reduced motion | Code + video tool | half a day |
| 12 | Put a CDN or edge cache in front (for example Cloudflare free tier); enable Brotli; fix HTML cache headers | You (DNS) + config | 2-4 h |
| 13 | Trim fonts and merge CSS | Code | 2 h |
| 14 | Expand thin answer pages; add pages: AI receptionist India pillar, WhatsApp business automation guide, individually written industry pages | Code + content | days |
| 15 | Merge /pricing and /answers/callplus-pricing roles (one canonical, the other short and linking); add real sources and dates to answer pages | Code | 2 h |
| 16 | Honest `lastmod` per page; drop `changefreq`/`priority`; remove the non-standard `Host:` line | Code | 30 min |

## Phase 4: Authority and monitoring (ongoing)

- Build presence off-site: YouTube demos, LinkedIn page, Wikidata entry, a corrected dev.to post.
- Add testimonials, case studies and sample call recordings once you have permission.
- Get a free Google PageSpeed API key to measure Core Web Vitals and the agent-readiness fraction; check field data after about 28 days of traffic.
- Re-run the audit after each batch of fixes and compare the score (72 now).
- Optional later: Content-Signal line, Markdown delivery, ai-catalog.json, WebMCP.
