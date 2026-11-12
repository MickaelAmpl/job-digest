# AI tells in English text: research for the cover-letter generator (9 Oct 2026)

Scope: patterns that make English text (especially cover letters) read as AI-written, turned into rules that a generator and code can check. Short letters, 230-380 words, mostly fixed paragraphs plus one model-written opening sentence.

Note on purpose: the Wikipedia guide warns that hiding these signs does not fix the underlying problem (generic, unverifiable content). The rules below are a quality filter: a letter must be specific, true to the candidate's facts, and tailored. Passing the rules does not prove a letter reads as human, and no detector score should be used as a gate.

## Sources

| Code | Source | URL | What it says |
|---|---|---|---|
| W | Wikipedia, "Wikipedia:Signs of AI writing" (WikiProject AI Cleanup; raw wikitext read in full, version as of Oct 2026) | https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing | Largest field guide: word lists by LLM "era", promotional and significance phrasing, negative parallelisms, rule of three, title case, bold and inline-header lists, emoji, curly quotes, hedging disclaimers, phrasal templates. Also lists "ineffective indicators" (perfect grammar, formal tone, transition words alone, bland prose) that should NOT be used as tests. Says detectors have non-trivial error rates. |
| K | Kobak et al., "Delving into LLM-assisted writing in biomedical publications through excess vocabulary" (preprint title: "Delving into ChatGPT usage in academic writing through excess vocabulary"), Science Advances 11(27), 2025 | https://arxiv.org/html/2406.07016v2 | Excess-word method on PubMed abstracts. The text names only ten high-weight excess words; it reports 319 excess style words in total (66% verbs, 16% adjectives in 2024). Full list NOT read (it is in the paper's tables or supplement). |
| L | Liang et al., "Mapping the Increasing Use of LLMs in Scientific Papers" (arXiv 2404.01268 v1) | https://arxiv.org/html/2404.01268v1 | Figure 2 caption gives the top 4 words most disproportionately used by LLMs vs humans in arXiv CS abstracts: realm, intricate, showcasing, pivotal. Surge from 2023. |
| PQ | PQ Magazine, "CV and cover letter AI red flags" (12 Jun 2025), quoting linguist Nicoli Westwood (WordDB) | https://www.pqmagazine.com/cv-and-cover-letter-ai-red-flags-2/ | Recruiting trade magazine. Flagged words: delve, foster, streamline, beacon, testament, underscore, facilitate. Example phrases: fostering innovation/collaboration, beacon of, testament to, by doing X I achieved Y, it's important to remember that. Also flags US spelling on UK CVs. |
| R | Reed (UK job board), "Seven ways to spot AI on a CV or covering letter" | https://www.reed.com/articles/seven-ways-to-spot-ai-on-a-cv-or-covering-letter | Recruiter view. Flawless but generic language; CV vs letter tone mismatch; US spellings on UK applications; vague achievements without metrics; repetitive sentence openers (Furthermore, Additionally, However); keyword stuffing from the job ad; invented skills. Says recruiters use these signs to probe in interview, not to reject outright. |
| SG | SEEK Grad (GradConnection), "How to identify AI-generated graduate resumes and cover letters" (17 Apr 2024) | https://au.gradconnection.com/blog/employers/post/how-to-identify-ai-generated-graduate-resumes-and-cover-letters/ | Employer guide. Repeated generic phrases (Furthermore, Moreover at paragraph starts); buzzwords (cutting-edge, highly innovative); over-sophisticated language; US spelling (analyzed, utilized); unnatural sentence structure. Recommends an AI detector as a first screen, while calling it not foolproof. |
| ILL | University of Illinois, Career Center, "AI" page | https://careercenter.illinois.edu/professionaldevadvice/ai | Write from your own material; use AI for feedback, not drafting; cover letters are narrative and AI is weak at personal storytelling; "AI produces the most likely response, not always the best". No phrase list. |
| SUF | Suffolk University Career Services, "Leveraging AI in your job search" | https://careers.suffolk.edu/leveraging-ai-in-your-job-search/ | Process: brainstorm, write your own draft, then ask AI to flag areas that are "too generic or could apply to many roles". Warns that a letter not in your own voice is "very obvious" to readers. No phrase list. |
| UCS | UC Strategies (editorial, Feb 2026), "I'm a recruiter, and this one detail tells me your resume was written by ChatGPT" | https://ucstrategies.com/news/im-a-recruiter-and-this-one-detail-tells-me-your-resume-was-written-by-chatgpt/ | Weak source. Author is a staff writer, not a recruiter; headline promises one detail but the body names none. Recurring buzzwords: "expert", "comfortable with technology", "cutting-edge". Stilted tone; lack of personal stories. |
| COU | Coursera, "5 AI cover letter red flags recruiters spot fast" (updated 6 Jan 2026) | https://www.coursera.org/articles/ai-cover-letter-red-flags-recruiters-spot-fast-video | Opened; text gives no specific phrases (examples are in an embedded video). Used only as a pointer. |

Not verified (seen only in search summaries, pages not opened): Kousha and Thelwall on "underscore" growth in PMC open-access papers (about 4% in 2022 to 20% in 2024, per a search summary); a university career-centre blog about three near-identical letters from one batch of applicants; a recruiter blog listing "I am excited to apply" and "I am a strong team player". Treat these as leads only.

Sources opened: 10 (W, K, L, PQ, R, SG, ILL, SUF, UCS, COU), of which 6 are recruiter or career-service pages (R, SG, PQ, ILL, SUF, COU) plus UCS as a weak one. The brief asked for 3-5 recruiter pages; I opened more because several gave no specific phrases.

## Vocabulary

Format: lowercase string, then the source codes that list it. "(count-only)" means a common word: do not ban it, cap its count per letter instead (see rules). Entries are only from opened sources. Phrases from the brief that no opened source lists are in the separate block after this list, not here.

- delve [W, PQ]
- delved [PQ]
- tapestry [W]
- testament [W, PQ]
- testament to [W, PQ]
- pivotal [W, L]
- crucial [W, K]
- showcase [W]
- showcasing [W, L]
- underscore [W, PQ]
- underscores [W, PQ]
- underscoring [W]
- vibrant [W]
- intricate [W, L]
- enduring [W]
- boasts [W]
- valuable [W]
- valuable insights [W]
- insights (count-only) [K]
- align with [W]
- resonate with [W]
- enhance [W]
- enhancing [W, K]
- fostering [W]
- foster [W, PQ]
- highlighting [W]
- highlight (count-only) [W]
- emphasizing [W]
- additionally [W, K]
- notably [W, K]
- key (count-only) [W]
- deep dive [W]
- robust [W]
- meticulous [W]
- meticulously [W]
- leverage [W]
- challenge (count-only) [W]
- multifaceted [W]
- prioritize [W]
- particularly (count-only) [K, W]
- exhibited [K]
- comprehensive [K]
- realm [L]
- streamline [PQ]
- beacon of [PQ]
- facilitate [PQ]
- stands as [W]
- serves as [W]
- functions as [W]
- operates as [W]
- represents a shift [W]
- marks a shift [W]
- a crucial role [W]
- a pivotal role [W]
- a vital role [W]
- a key role [W]
- a significant role [W]
- key turning point [W]
- evolving landscape (count-only) [W]
- landscape (count-only) [W]
- focal point [W]
- indelible mark [W]
- deeply rooted [W]
- setting the stage for [W]
- contributing to the [W]
- reflects broader [W]
- profound [W]
- groundbreaking [W]
- renowned [W]
- nestled [W]
- in the heart of [W]
- diverse array [W]
- commitment to [W]
- exemplifies [W]
- associated with [W]
- connected to [W]
- connected with [W]
- in association with [W]
- particularly associated [W]
- industry reports [W]
- observers have cited [W]
- experts argue [W]
- some critics argue [W]
- independent coverage [W]
- trade publications [W]
- active social media presence [W]
- despite its [W]
- faces several challenges [W]
- despite these challenges [W]
- future outlook [W]
- challenges and legacy [W]
- important to note [W]
- not publicly available [W]
- maintains a low profile [W]
- carefully reviewed [W]
- apologizing for any inconvenience [W]
- i hope this message finds you well [W]
- i am writing to [W]
- we remain committed to [W]
- thank you for your understanding [W]
- furthermore [R, SG]
- moreover [SG]
- however (count-only, paragraph opener) [R]
- cutting-edge [SG, UCS]
- highly innovative [SG]
- expert (count-only) [UCS]
- comfortable with technology [UCS]
- orchestrated a dynamic team [R]
- fostering a culture of excellence [R]
- strategic implementation [R]
- stakeholder management [R]
- agile methodology [R]
- cross-functional collaboration [R]
- fostering innovation [PQ]
- fostering collaboration [PQ]
- it's important to remember [PQ]

Count of entries above: 116 (checked by script after writing).

### Unverified candidates (house list, NOT in any opened source)

These came from the brief or from my own reasoning. They are not backed by a source I opened, so they are not in the vocabulary above. Product can still ban them as a house style choice, but label the rule as a house rule.

- thrilled, eager to contribute, keen interest, i am confident that, in today's fast-paced, ever-evolving, embark, navigate, elevate, spearheaded, unwavering, commendable, invaluable, seamless, seamlessly (appears only inside a W example quote, not in W's word list), plays a crucial role (W has "a crucial role" only), i am excited to apply, i look forward to the opportunity, proven track record, detail-oriented, team player, passionate, results-driven, as an ai language model, i hope this helps.

## Structural patterns

Each: what it is, a made-up example (not copied from any source), and whether code can check it.

1. Negative parallelism, "not X, but Y" / "not only X but also Y" / "it's not X, it's Y" [W]. Example: "This is not a list of tasks, but a record of ownership." Code: yes, regex (rule A7).
2. "Y rather than X" contrast [W]. Example: "I built the process rather than just using it." Code: yes, regex, but common in human text; cap at 1 per letter.
3. Rule of three lists, especially adjective triples and "X, Y, and Z" phrases [W, R]. Example: "fast, reliable, and clear." Code: approximate only (regex count of "word, word, and word"); a warning, not a fail.
4. Summary sentence after a point, restating what was just said [W "section summaries"]. Example: "This experience taught me the value of clear communication." Code: yes, regex on taught-me / shows-that openers.
5. Participle tail adding vague significance: ", highlighting the importance of ..." [W "superficial analyses"]. Example: "...across three countries, showing the reach of the programme." Code: yes, regex on ", <ing-verb>" after a sentence.
6. Sentence openers that are transitions in a row: Furthermore, Moreover, Additionally, Consequently, Notably, However [W, R, SG]. Code: yes, count per letter and no two paragraphs starting with the same opener.
7. Repeated sentence openers or identical sentence rhythm [R, SG]. Example: three sentences in a row starting "I ..." Code: partly (count first word of each sentence; flag repeats of 3 in a row). Paragraph-length uniformity is NOT in any opened source; treat as a house heuristic, not a proven tell.
8. Em dashes and colons in place of commas [W "overuse of em dashes"; W also says the overuse has shifted and that newer models now avoid them, so absence alone is not proof]. Example: "I led the rollout - and it worked." Code: yes, count of U+2014 and U+2013 (zero allowed; hyphen only, per Misha's rule).
9. Title Case headings and bold inline headers [W "title case", "overuse of boldface", "inline-header vertical lists"]. Example: "**Key Skills:** Python, SQL". Code: yes, regex for markdown bold and for lines ending with a colon in the letter body.
10. Emoji as formatting [W]. Code: yes, Unicode emoji regex.
11. Curly quotes and curly apostrophes [W; W notes macOS and Word smart-quote settings can produce them, so they do not prove AI]. Example: a right single quote in "I\u2019m". Code: yes, regex on U+2018, U+2019, U+201C, U+201D. Fix by normalising to straight characters before the check.
12. Markdown in a letter that should be plain text [W "use of Markdown"]. Example: lines starting with "- " or "**". Code: yes.
13. Vague attribution: "industry reports", "experts argue", "observers have cited" [W]. Example: "Experts agree that data quality matters." Code: yes, regex list.
14. Significance inflation: "a crucial role", "a pivotal moment", "setting the stage for", "a testament to" [W, PQ]. Code: yes, from the vocabulary list.
15. Promotional tone toward the employer: "renowned", "groundbreaking", "commitment to excellence" [W]. Example: "Your groundbreaking work in fintech inspires me." Code: yes, vocabulary list plus a rule: at most one praise sentence about the company, and it must contain a fact from the input (prompt and human check).
16. Connection language: "associated with", "connected to" used where a plain verb would do [W]. Example: "I was associated with the onboarding team." Code: yes, regex.
17. Copula avoidance: "serves as", "features", "boasts", "offers" in place of "is" or "has" [W]. Example: "The team boasts strong analytics skills." Code: yes, regex. Note W: human text uses "is" and "has" far more, so these are real signals.
18. Hedging stacks: "may", "might", "could", "potentially", "likely", "important to note" [W "hedging disclaimers", "didactic disclaimers"]. Example: "This may potentially help the team." Code: yes, count per letter (cap 2) and the phrase list.
19. Generic closers and openers: "I am excited to apply", "I look forward to the opportunity", "I hope this message finds you well" [W phrasal templates for the last one; the first two are unverified house candidates]. Code: yes, exact phrase list.
20. Vague accomplishments with no number, name, or date [R, ILL, SUF]. Example: "I improved team efficiency through strategic initiatives." Code: partly, each body paragraph must contain a digit or a proper noun other than the first word (house rule; the source backs the idea).
21. Overly polished, flawless, bland prose with no idiosyncrasy [R, W "bland" is an ineffective indicator, UCS]. Example: a letter where every sentence is medium length and grammatically clean. Code: no. Prompt and human only.
22. Mirroring the job ad wording too closely, keyword stuffing [R, UCS]. Example: "...stakeholder management, agile methodology, and cross-functional collaboration." Code: yes, shared 4-word sequences with the ad text (rule A14).
23. Tone mismatch between the CV and the letter [R]. Code: no. Prompt and human only.
24. Invented skills, tools, or projects [R calls this the most dangerous sign]. Code: no. Human check against the candidate's facts; the generator must not add facts.
25. Stock "AI" vocabulary clusters: several listed words in one short text [W: "one or two may be coincidental, but many together is a strong tell"]. Code: yes, count distinct vocabulary hits per letter.
26. US spelling in a UK or AT-targeted letter, or the reverse [R, SG, PQ]. Example: "optimized" in a British letter. Code: yes, with a per-target spelling list (rule A11).
27. Lists of bullets all starting with a verb in a letter [R]. Code: yes, if the letter has bullets (the generator should have none).

Signs that are NOT tells (per W "ineffective indicators" and the Illinois page): perfect grammar, formal register alone, transition words used once, unsourced content, correct formatting. Do not write rules that ban these.

## Cover-letter specific tells

What recruiters and career services say gives an AI letter away:

- Generic praise of the company that could be pasted into any application [R, ILL, SUF]. Test: a sentence about the company should contain one fact from the input.
- Copying the job ad closely; a CV that reads like a checklist of the job description [R]. Test: shared word sequences with the ad (rule A14).
- Flawless, stilted, over-formal tone with no personal voice [R, UCS, W].
- Vague achievements with no metrics, names, or dates [R].
- Tone or vocabulary that changes sharply between the CV and the letter [R].
- Buzzwords: "expert", "comfortable with technology", "cutting-edge", "highly innovative", "dynamic team" [UCS, SG, R].
- Repeated paragraph openers: Furthermore, Moreover, Additionally, However [SG, R].
- US spelling on a UK or Austrian application [R, SG].
- Claims that cannot be checked, or skills that look invented [R].
- Letter not in the candidate's own voice; AI writing the whole letter [SUF, ILL].
- Identical letters across applicants [snippet only, not verified in an opened page]. Design risk for us: fixed paragraphs reused verbatim across many applications could be noticed if a recruiter compares letters. Mitigation proposal: 2-3 variants per fixed paragraph, and one company-specific sentence per application. This is a product decision.
- Recruiters use these signs to decide what to probe in interview, not to reject outright [R]. The detector advice in SG and the tool in UCS are weak: W says detectors have non-trivial error rates, so do not use detector scores as a gate.

## Rules for our generator

Letter must pass all (a) rules. (b) rules are for the prompt and for human review, not code.

### (a) Checkable in code

Setup: normalise curly quotes to straight, strip nothing else. Word count is whitespace-split body text.

- A1. Word count between 230 and 380 (body only, excluding the sign-off). Fail outside.
- A2. Zero em dashes and zero en dashes: `[\u2013\u2014]`. Fail if any. Hyphens only. (Matches Misha's rule.)
- A3. Zero curly quotes or apostrophes: `[\u2018\u2019\u201C\u201D]`. Fail if any.
- A4. Zero emoji: `[\U0001F300-\U0001FAFF☀-➿]`. Fail if any.
- A5. No Markdown: fail on `\*\*`, `__`, `^#{1,6}\s`, `^\s*[-*+]\s`, `^\s*\d+[.)]\s`, backticks, or a line that is only a title followed by a colon.
- A6. Banned phrases, case-insensitive, word-boundary match. Fail on any hit from the vocabulary list marked with a source code, EXCEPT the count-only items (rule A8). Include all stems from the list (e.g. `foster|fosters|fostering`, `underscore[sd]?|underscoring`, `showcas(e|es|ing)`, `enhanc(e|es|ing)`, `highlight(s|ing)?` only when followed by "its importance").
- A7. Negative parallelism: fail on `\bnot only\b[^.!?]{0,80}\bbut\b`, on `\bnot\b[^.!?]{0,60}?,\s*but\b`, and on `\b(it'?s|it is) not\b[^.!?]{0,60}?,\s*(it'?s|it is)\b`.
- A8. Count-only words: cap at 1 each per letter, and cap the total of count-only hits at 3. Words: across, within, key, landscape, practical, matters, expert, however, notably, particularly, insights, challenge, highlight.
- A9. Participle tail: fail on `,\s+(highlighting|underscoring|emphasizing|emphasising|reflecting|showcasing|ensuring|demonstrating|contributing|fostering|enhancing|symbolizing|symbolising|cultivating|encompassing|reinforcing|illustrating)\b`.
- A10. Copula avoidance: fail on `\b(serves|stands|functions|operates) as\b`, `\bboasts\b`, `\bfeatures (a|an|the)\b`, `\bmarks (a|an|the)\b`, `\brepresents (a|an)\b`. Also count "is" and "has" to confirm they appear (warn if zero).
- A11. Spelling variety: one variant per letter, per target language set by product. Keep two lists (UK: -ise/-isation/-our/-centre/programme/analyse; US: -ize/-ization/-or/-center/program/analyze). Fail on mixing. Note: the target choice is a product question (see end of file).
- A12. Summary sentence: fail on `\b(this|these|that) (experience|role|work|background|project|journey|time|period)s? (taught|has taught|showed|has shown|demonstrates|highlights|underscores)\b` and on `\b(taught me|has taught me)\b`.
- A13. Hedging: fail if the letter contains more than 2 hits of `\b(may|might|could|potentially|perhaps|likely|possibly)\b`, or any of: "important to note", "it's worth noting", "it should be noted".
- A14. Job-ad mirroring: fail if any 5 consecutive words (lowercased, punctuation removed) appear in the job ad. Warn if any 4-word sequence appears more than once in the letter. Only runs when the ad text is supplied.
- A15. Transition openers: fail if any paragraph or sentence begins with Furthermore, Moreover, Additionally, Consequently, Notably, or However. Count "however" at most once.
- A16. Specific fact per body paragraph: each body paragraph contains at least one digit or one capitalised word that is not the first word of the sentence (house rule, backed by R, ILL, SUF). Fail on a paragraph with neither.
- A17. Company named at most twice in the whole letter. Fail on three or more mentions. Count is case-insensitive.
- A18. Vague attribution: fail on `\b(industry (reports|experts|analysts)|experts (argue|say|agree)|observers|some critics|many believe|studies (show|suggest)|research (shows|suggests))\b`.
- A19. Sign-off and greeting are fixed templates from the approved list; fail if the sign-off is not one of them (keeps repeated generator output in a known set).
- A20. Sentence-start repetition (house heuristic): fail if three consecutive sentences start with the same word. Warn if more than 40% of sentences start with "I".

The 10 strongest code rules (for the report): A2 (dashes), A3 (curly quotes), A4 (emoji), A5 (Markdown), A6 (banned list), A7 (negative parallelism), A9 (participle tail), A10 (copula avoidance), A14 (ad mirroring), A16 (specific fact per paragraph).

### (b) Prompt-only and human review

- B1. Write in plain first person. Use only facts from the candidate's input. Never invent a number, a name, a tool, a result, or a project. If a fact is missing, write a visible placeholder like [CHECK: team size] and do not guess.
- B2. The company sentence must contain one concrete, checkable fact from the input (a product, a market, a stated goal). No generic praise.
- B3. Do not paraphrase the job ad into the letter. Refer to the role by its title only.
- B4. Plain verbs: is, has, did, built, ran, led. No dressing up.
- B5. One concrete example per body paragraph, with a number or a name where the input has one.
- B6. Keep the letter in one voice across paragraphs; do not shift between formal and casual.
- B7. No summary of the previous paragraph, no closing line that restates the letter, no "this shows that".
- B8. Do not use hedges to soften claims; state what is known, and leave out what is not.
- B9. The closing asks for a conversation or an interview in one plain sentence, no extra enthusiasm.
- B10. Human check before sending: remove any sentence that could be pasted into another company's letter.
- B11. Human check: every claim matches the candidate's CV. Invented skills are the most serious failure (R).
- B12. Human check: read the letter next to the CV; the tone and vocabulary should match.

## Open questions for the product

1. UK or US spelling, or one per target (Austria, UK, US)? Rule A11 needs the answer.
2. Are the fixed paragraphs reused word for word across applications? If yes, decide on the variant rotation (see Cover-letter section).
3. Keep the house-only candidates (unverified block above) as banned words, or drop them? Recommendation: keep as house rule, labelled as house.
4. Do we want a second opened source for the count of identical letters and the detector question? Not verified; not needed for the rules above.

## Verification note

- Wikipedia text was read from the raw wikitext export (cleaned of markup into a scratch file), full length, all sections. Quotes are short phrases only.
- All URLs above were opened. Kobak et al. full word list was not available in the pages opened, so only the ten words in the text are recorded.
- Vocabulary list: 116 entries, each traced to a source code in the Sources table.
