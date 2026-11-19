"""AI-sounding writing in a spoken script (design 8.5 item 5; FAILURE_MODES.md, script_lint). Every finding blocks the
script, which goes back for a rewrite. The lists are our own; words our brands can mean literally stay off them."""
from __future__ import annotations

import re

STOCK = ["at the end of the day", "in today's world", "in this day and age", "let's dive in", "it's worth noting",
         "needless to say", "the truth is", "here's the thing", "let that sink in", "buckle up", "picture this",
         "imagine a world", "without further ado", "a testament to", "plays a crucial role", "in conclusion",
         "whether you're a",
         # the copywriting skill's AI-tells list (coreyhaines31/marketingskills, MIT; operator, 5 Nov)
         "when it comes to", "here's what nobody tells you", "let me be clear", "changes everything", "moreover",
         "additionally", "importantly", "say goodbye to", "next level", "studies show", "research shows",
         "experts agree", "experts say"]
SUSPENSE = ["you won't believe", "here's where it gets interesting", "but here's the twist", "plot twist:",
            "spoiler:", "but wait, there's more", "what happens next will", "and that's when everything changed",
            "the best part?"]
BUZZ = (r"delv(?:e|es|ed|ing)|tapestr(?:y|ies)|realms?|myriad|plethora|multifaceted|paradigms?|synerg(?:y|ies)"
        r"|embark(?:s|ed|ing)?|unleash(?:es|ed|ing)?|game[- ]changers?|cutting[- ]edge|seamless(?:ly)?"
        r"|elevat(?:e|es|ed|ing)|empower(?:s|ed|ing)?|testament|beacons?|meticulous(?:ly)?|intricate(?:ly)?"
        r"|utiliz(?:e|es|ed|ing)|leverag(?:e|es|ed|ing)|supercharg(?:e|es|ed|ing)|turbocharg(?:e|es|ed|ing)"
        r"|ever-evolving")
REVEAL = r"(?:best|worst|real|hard|funny|strange|weird|surprising|good|bad) (?:part|thing|news)|catch|kicker" \
         r"|twist|truth|secret|answer|result|problem|point|key|trick"  # the nouns a colon reveal hangs on


NOT = (r"it's not|it is not|that's not|this is not|isn't|wasn't|aren't|doesn't|don't"
       r"|you're not|they're not|we're not|you aren't|they aren't")
IS = r"it's|it is|it was|you're|they're|we're"  # the correction repeats its subject; "That's why" is a follow-up


def _phrases(items: list[str]) -> str:
    return "|".join(r"\b" + re.escape(p) + (r"\b" if p[-1].isalnum() else "") for p in items)


RULES = [
    ("em_dash", r"—|\s–\s|\s--?\s|\w--\w"),  # a spaced hyphen is a dash too; hyphenated words and 3-4 are fine
    ("semicolon", r";"),
    ("contrast_cliche", r"\bnot just\b[^.?!]*?\bbut\b"
                        rf"|\b(?:{NOT})\b[^.?!;]{{1,60}}?[,—–]\s*(?:{IS}|that's|this is)\b"
                        rf"|\b(?:{NOT})\b[^.?!;]{{1,60}}?[.!]\s+(?:{IS})\b"),  # "This isn't X. It's Y."
    ("stock_phrase", _phrases(STOCK)),
    ("fake_suspense", _phrases(SUSPENSE)),
    ("buzzword", rf"\b(?:{BUZZ})\b"),
    ("negation_list", r"\bno\s+\w+(?:\s+\w+)?,\s*no\s+\w+(?:\s+\w+)?,?\s+(?:and\s+|or\s+)?no\s+\w+"),  # 3 "no"s
    ("qa_reveal", r"(?:^|(?<=[.!?]\s)|(?<=\n))(?:(?:and|so|but)\s+)?"  # "The result? Three left." "Why? Because"
                  r"(?:(?:the|your|my|our|their)\s+\w+(?:\s+\w+)?|why)\?\s+\S"),
    ("colon_reveal", rf"\bthe\s+(?:{REVEAL})\s*:"),  # "The catch: ..." "The best part: ..."
    # told-story tells from the "Don't Yawn" and "Hold the Door" pilots (operator, 6 Nov: "the script felt AI written")
    ("narrator_tag", r"(?:^|(?<=[.!?]\s))[^.!?\n]{1,60},\s+I\s+(?:figured|guessed|thought|supposed|reckoned"
                     r"|was sure|remembered|realized)\s*[.:!]"),  # "Screaming, I guessed." "A trap, I was sure."
    # "my mouth, opening.": a verbless line, so the noun phrase opens it; "hit the floor, snoring." is a sentence
    ("trailing_fragment", r"(?:^|(?<=[.!?:,]\s))(?:my|the|his|her|their|its|our|your)\s+\w+(?:\s+\w+)?,\s+\w+ing[.!]"),
]
STORY_RULES = [  # a told story says everything as a spoken sentence; an explainer may still use a colon
    ("colon", r"(?<!\d):(?!\d)"),  # "I peeked: mouths wide open." (12:30 is a time)
]


def lint(text: str, *, story: bool = False) -> list[dict]:
    text = text.replace("’", "'").replace("‘", "'")
    found = [{"rule": rule, "evidence": text[max(0, m.start() - 30):m.end() + 30].strip()}
             for rule, rx in RULES + STORY_RULES * story for m in re.finditer(rx, text, re.IGNORECASE)]
    split = r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<=[.?!])\s+|\n+"  # honorifics end no sentence
    sentences = [s.strip() for s in re.split(split, text) if s.strip()]
    words = [re.findall(r"[A-Za-z0-9']+", s.lower()) for s in sentences]
    single = [w[0] if len(w) == 1 else None for w in words]
    pair = next((i for i in range(len(single) - 1) if single[i] and single[i + 1] and single[i] != single[i + 1]
                 and not (single[i].isdigit() and single[i + 1].isdigit())), None)  # "Click. Click." "35. 37." count
    if pair is not None:  # one-word lines back to back are drama; a lone "No." or "Done." is speech
        found.append({"rule": "one_word_drama", "evidence": " ".join(sentences[pair:pair + 3])})
    return found
