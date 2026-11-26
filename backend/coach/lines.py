"""Offline line banks: persona -> moment -> several phrasings, so the coach never sounds like a script.
Slots: {nm} = ", Name" (or empty) and {tod} = Morning/Afternoon/Evening are always available; others per SLOTS."""
from __future__ import annotations

import random
import re
from datetime import datetime

COMMON = {"nm", "tod"}
SLOTS: dict[str, set[str]] = {
    "open_low": set(), "open_ok": set(), "open_fresh": set(),
    "intro": {"day", "cnt", "ex"}, "warmup": {"ex"},
    "react_easy": {"w", "r"}, "react_good": {"w", "r"}, "react_grind": {"w", "r"}, "react_miss": {"r"},
    "react_pr": {"pr_ex", "w", "r"},
    "inst_up": {"nw", "nr"}, "inst_down": {"nw", "nr"}, "inst_same": {"nw", "nr"},
    "rest_cue": {"rest"},
    "ex_done": {"ex", "nxt", "nsets", "nreps"}, "ex_done_last": {"ex"},
    "rest_over": {"ex", "n", "tot"},
    "end": {"sets", "exn", "tonnes"}, "end_pr": {"prs"}, "end_prot": {"prot"},
    "pain": set(), "skip": {"ex", "nxt"}, "skip_last": {"ex"},
    "fit": {"min", "cnt", "sets"}, "status": {"ex", "n", "tot", "rem"},
    "fallback": set(), "ask_time": set(), "ready_go": {"ex", "n", "tot"}, "sample": set(),
    "last_time": {"ex", "last"}, "last_time_short": {"last"}, "no_plan": set(), "already_done": set(), "resume": {"done", "ex", "n", "tot"},
    "nothing_logged": set(), "rest_done": set(), "all_done": set(),
}

BANKS: dict[str, dict[str, list[str]]] = {
    "hype": {
        "open_low": ["{tod}{nm}! Readiness is a bit low today, so we're gonna train smart, not stupid. I've got you.",
                     "Okay, your body's telling me it's tired. We go a little lighter and still win the session.",
                     "Not your freshest day on paper. No stress, we adjust as we go."],
        "open_ok": ["{tod}{nm}! You're rested, I'm ready, there's iron to move. Let's get after it.",
                    "Look at you, fresh and recovered. Today's a good day to push a little.",
                    "Readiness looks great. Warm up well and let's have some fun."],
        "open_fresh": ["{tod}{nm}! No check-in today, so I'll trust you feel good. Let's go!",
                       "Hey{nm}! I don't have a check-in for you, so shout if anything feels off."],
        "intro": ["{day} on the menu. {cnt} exercises, and we open with {ex}.",
                  "{day} day! {cnt} lifts, starting with {ex}."],
        "warmup": ["Do a couple of lighter warm-up sets first, I won't count those. Then tell me your first working set.",
                   "Warm up with some easy sets first, then just tell me what you did when the real work starts."],
        "react_easy": ["Ooh, that looked easy. {w} for {r}, smooth.", "Clean! That was light work for you.",
                       "That moved fast. You've got more in the tank."],
        "react_good": ["Solid set! {w} for {r}, right where we want it.", "Yes! That's the stuff.",
                       "Nice work, that's exactly the effort I want."],
        "react_grind": ["Whoa, that was a grinder! Respect for pushing through.", "Ooh, that one fought back. Nice grit!",
                        "That was a war. You earned that one."],
        "react_miss": ["No worries, that one got heavy. We reset and go again.", "Missed reps happen to everyone. Shake it off.",
                       "Not a problem. We adjust and attack the next set."],
        "react_pr": ["Hold on, that's a PR! New best on {pr_ex}! Let's go!",
                     "Personal record! You just beat your best. How does that feel?",
                     "That's a PR, are you kidding me! Huge."],
        "inst_up": ["Let's bump it to {nw} for {nr}.", "Go {nw} for {nr} next, you've earned it."],
        "inst_down": ["Let's drop to {nw} for {nr} and own it.", "Back it off to {nw} for {nr}, quality reps."],
        "inst_same": ["Same again, {nw} for {nr}.", "Keep it at {nw} for {nr}, you're dialed in."],
        "rest_cue": ["Take {rest}, I've got the clock.", "Breathe. {rest}, then back at it."],
        "ex_done": ["And that's {ex} done! Up next, {nxt}, {nsets} sets of {nreps}.", "{ex}, crushed. Moving on to {nxt}."],
        "ex_done_last": ["That's the last one, {ex} is done! Say wrap up whenever you're ready and I'll give you the recap.",
                         "And that's everything! Great session. Say wrap up when you're done."],
        "rest_over": ["Rest's up! {ex}, set {n} of {tot}. Let's go.", "Time! {ex}, set {n} of {tot}. Ready when you are."],
        "end": ["That's a wrap! {sets} sets across {exn} exercises, {tonnes} tonnes moved. You showed up and crushed it.",
                "Session done! {sets} sets, {exn} exercises, {tonnes} tonnes in total. Proud of that work."],
        "end_pr": ["Oh, and don't forget, {prs}!", "And you hit {prs} today. Remember that."],
        "end_prot": ["You've still got about {prot} grams of protein to hit today. A shake or a good meal will do it."],
        "pain": ["Okay, stop right there. Pain is not something we push through. Which movement was it?"],
        "skip": ["No problem, skipping {ex}. Next up is {nxt}.", "Skipping {ex}. We keep moving, {nxt} is next."],
        "skip_last": ["Skipped {ex}. That was the last one, so say wrap up when you're ready."],
        "fit": ["Got it, {min} minutes. I trimmed it to {cnt} exercises and {sets} sets. Main lifts stay, we move fast."],
        "status": ["You're on {ex}, set {n} of {tot}. {rem} sets left overall."],
        "fallback": ["Hmm, I didn't catch a set in that. Tell me the exercise, weight and reps, like bench 80 for 8.",
                     "Say it like squat 100 for 5 and I'll log it."],
        "ready_go": ["Let's go! {ex}, set {n} of {tot}."],
        "ask_time": ["No problem! How many minutes do you have?"],
        "sample": ["Hey, I'm Max! I'll be in your ear the whole session, cheering you on and keeping you honest."],
    },
    "mentor": {
        "open_low": ["{tod}{nm}. Your numbers say you're running a bit low today. That's fine. We'll let the effort guide us.",
                     "Take a breath. Recovery looks lower than usual, so today is about quality, not heroics.",
                     "A gentle start today. Your body's asking for some care, so we'll keep the effort honest."],
        "open_ok": ["{tod}{nm}. You're well recovered today, which is a good place to train from.",
                    "Good to see you. Everything looks in order, so let's build on last time.",
                    "Nice and fresh. Settle in, warm up properly, and we'll take it set by set."],
        "open_fresh": ["{tod}{nm}. I don't have a check-in for you, so tell me if anything feels off as we go.",
                       "Hello{nm}. No check-in today, so let's pay extra attention to how you feel."],
        "intro": ["We've got {day} today. {cnt} exercises, and we'll begin with {ex}.",
                  "Today is {day}: {cnt} movements, starting with {ex}. No rush."],
        "warmup": ["Warm up properly first. A few lighter sets, nothing taxing. Then tell me when you start working.",
                   "Take your time warming up. When the real sets begin, tell me what you did."],
        "react_easy": ["That looked comfortable. Good control.", "Nice and smooth. You had plenty left there.",
                       "Easy work for you. That's a good sign."],
        "react_good": ["Good set. That's the right effort.", "Well done. Steady and controlled.",
                       "That's the sweet spot. Hard enough to count, not so hard it costs you."],
        "react_grind": ["That was heavy. You pushed through it well.", "A real grind. Good effort, and let's be sensible with the next one.",
                        "Hard-earned. Take your time recovering."],
        "react_miss": ["That's okay. Not every set goes to plan, and that's information, not failure.",
                       "It happens. We'll take a little off and rebuild.", "Let that one go. The next set is a fresh start."],
        "react_pr": ["That's a new personal best on {pr_ex}. You should be proud of that.",
                     "A PR. That's months of work showing up right there.", "New best. Consistency does that, remember it."],
        "inst_up": ["Let's go up to {nw} for {nr} on the next one.", "For the next set, try {nw} for {nr}."],
        "inst_down": ["I'd drop to {nw} for {nr} so the reps stay clean.", "Ease down to {nw} for {nr} on the next one."],
        "inst_same": ["Stay right there, {nw} for {nr}.", "Same weight again, {nw} for {nr}."],
        "rest_cue": ["Rest about {rest}. I'll let you know when it's time.", "Take {rest} and let your breathing settle."],
        "ex_done": ["That finishes {ex}. Next is {nxt}, {nsets} sets of {nreps}.", "Good, {ex} is done. Take a moment, then {nxt}."],
        "ex_done_last": ["That's {ex} finished, and that's the whole session. Say wrap up when you're ready and we'll review it."],
        "rest_over": ["That's your rest. {ex}, set {n} of {tot}, whenever you're ready.",
                      "Rest is up. Take one more breath, then {ex}, set {n} of {tot}."],
        "end": ["Session complete. {sets} sets, {exn} exercises, about {tonnes} tonnes in total. Well done for showing up today.",
                "That's the work done. {sets} sets across {exn} exercises, {tonnes} tonnes. Good session."],
        "end_pr": ["Worth noting: {prs}.", "And a highlight: {prs}."],
        "end_prot": ["For recovery, aim to get roughly {prot} more grams of protein in today."],
        "pain": ["Let's stop that movement. Pain is a signal worth listening to. Where exactly does it hurt?"],
        "skip": ["Okay, we'll leave {ex} for today. Moving on to {nxt}.", "That's fine. Skip {ex}, and we continue with {nxt}."],
        "skip_last": ["We'll leave {ex}. That was the last one, so say wrap up when you're ready."],
        "fit": ["Understood. I've fitted today to {min} minutes: {cnt} exercises, {sets} sets, main lifts kept."],
        "status": ["You're on {ex}, set {n} of {tot}, with {rem} sets still to go."],
        "fallback": ["I didn't quite catch that. Tell me the exercise, the weight and the reps.",
                     "Say it plainly, like bench press 80 for 8, and I'll log it."],
        "ready_go": ["Alright. {ex}, set {n} of {tot}. Take your time."],
        "ask_time": ["That's fine. How much time do you have?"],
        "sample": ["Hi, I'm Sage. I'll be with you through the session, one set at a time."],
    },
    "straight": {
        "open_low": ["{tod}{nm}. Readiness is low. Not a disaster, just don't be a hero. We'll scale it.",
                     "You look tired on paper. So we do the work, just a little smarter.",
                     "Low readiness. Slept badly, didn't you. Fine. We adjust and still train."],
        "open_ok": ["{tod}{nm}. Recovered and ready. No excuses then.", "Readiness is good. Try not to waste it.",
                    "You look fine. Let's see if the bar agrees."],
        "open_fresh": ["{tod}{nm}. No check-in, so I'm flying blind. Speak up if something hurts.",
                       "No check-in. I'll assume you're fine. Prove me right."],
        "intro": ["{day}. {cnt} exercises. First up, {ex}.", "It's {day}. {cnt} exercises, {ex} first. Let's go."],
        "warmup": ["Warm up first. Lighter sets, I'm not logging them. Then give me your first real set.",
                   "Do your warm-up. Skipping it is how people get hurt. Then tell me your first work set."],
        "react_easy": ["That was easy. Obviously.", "Barely a sweat. Noted.", "Fine. That's too light for you."],
        "react_good": ["Good. That's the target.", "Right on the money.", "Okay, that's actually good."],
        "react_grind": ["That looked ugly. Respect, but ugly.", "A grinder. We'll back off a touch.",
                        "You nearly lost that one. Let's not make a habit of it."],
        "react_miss": ["Short on reps. We'll fix it.", "Missed it. Drop the weight, ego stays outside.", "Happens. Don't dwell on it."],
        "react_pr": ["That's a PR. Don't let it go to your head.", "New best on {pr_ex}. Fine, I'll say it: well done.",
                     "PR. Good. Now do it again."],
        "inst_up": ["Next: {nw} for {nr}.", "Add weight. {nw} for {nr}."],
        "inst_down": ["Drop to {nw} for {nr}.", "Take weight off. {nw} for {nr}."],
        "inst_same": ["Same weight. {nw} for {nr}.", "Repeat it. {nw} for {nr}."],
        "rest_cue": ["Rest {rest}. I'll shout when it's up.", "{rest}. Don't scroll your phone, I'm timing it."],
        "ex_done": ["{ex} done. Next, {nxt}, {nsets} sets of {nreps}.", "That's {ex}. {nxt} is next."],
        "ex_done_last": ["{ex} done. That's everything. Say wrap up and I'll give you the numbers."],
        "rest_over": ["Time. {ex}, set {n} of {tot}.", "Rest is over. {ex}, set {n} of {tot}. Get under the bar."],
        "end": ["Done. {sets} sets, {exn} exercises, {tonnes} tonnes. Not bad.",
                "That's the session. {sets} sets across {exn} exercises, {tonnes} tonnes. Decent work."],
        "end_pr": ["Also, {prs}. Don't get cocky.", "And {prs}. Noted."],
        "end_prot": ["You're about {prot} grams of protein short today. Eat something."],
        "pain": ["Stop. We're not lifting through pain. What hurts, and where?"],
        "skip": ["Skipping {ex}. {nxt} next.", "Fine, {ex} is off the list. On to {nxt}."],
        "skip_last": ["Skipped {ex}. That was the last one. Say wrap up."],
        "fit": ["{min} minutes. Fine. {cnt} exercises, {sets} sets, the important stuff stays."],
        "status": ["{ex}, set {n} of {tot}. {rem} sets left."],
        "fallback": ["Didn't get that. Exercise, weight, reps.", "Try again. Like this: squat 100 for 5."],
        "ready_go": ["{ex}, set {n} of {tot}. Go."],
        "ask_time": ["Okay. How many minutes?"],
        "sample": ["I'm Ash. I'll tell you what to lift and when. Try to keep up."],
    },
}

# Plain informational lines that are the same for every English persona.
_EN_COMMON = {
    "last_time": ["Last time on {ex} you did {last}."],
    "last_time_short": ["Last time you did {last}."],
    "no_plan": ["Set up your plan first, then come find me."],
    "already_done": ["You've already done everything for today. Say wrap up and I'll give you the recap."],
    "resume": ["Back already. You're {done} sets in, so we pick up at {ex}, set {n} of {tot}.", "Welcome back. {done} sets are done. Next is {ex}, set {n} of {tot}."],
    "nothing_logged": ["Nothing logged today, so there's no recap. Come back when you've lifted."],
    "rest_done": ["Rest is up. That's everything, so say wrap up whenever you're ready."],
    "all_done": ["You're all done. Say wrap up when you're ready for the recap."],
}
for _p in ("hype", "mentor", "straight"):
    BANKS[_p].update(_EN_COMMON)

# Pehalwan: a Haryanvi akhada coach. Written in Devanagari so a Hindi (hi-IN) voice reads it correctly.
# Haryanvi markers: सै (is), कोनी / नी (not), घणा (very/a lot), तनै / मनै (to you / to me), धोरे (with), कित (where), सूं (am), तै (from/than).
BANKS["haryanvi"] = {
    "open_low": ["{tod} की राम राम{nm}! आज तेरा शरीर थोड़ा थक्या सै, तो आज समझदारी तै काम लेंगे। मैं सूं ना तेरे साथ।",
                 "ओ भाई, रिकवरी आज कम दिखै सै। कोई बात नी, हल्का-हल्का चलांगे, पर सेशन पूरा करांगे।",
                 "आज शरीर थोड़ा ढीला सै तेरा। जोर मत लगाइये, आराम तै चलांगे अर काम पूरा करांगे।"],
    "open_ok": ["{tod} की राम राम{nm}! आज तो तेरी बॉडी एकदम तैयार दिखै सै। चाल, आज घणा जोर लगावांगे!",
                "वाह रै शेर! रिकवरी बढ़िया सै, आज थोड़ा और धकेलण का दिन सै।",
                "रिकवरी एकदम चौकस सै। गर्म हो ले, फेर लोहा उठावांगे!"],
    "open_fresh": ["{tod} की राम राम{nm}! आज चेक-इन कोनी करया, तो मैं मान ल्यूं सूं तू ठीक सै। कुछ गड़बड़ लागे तो बता दीये।",
                   "राम राम{nm}! तेरा चेक-इन कोनी मिल्या, इस खातर जो भी तकलीफ हो, बीच में बता दीये।"],
    "intro": ["आज {day} सै। {cnt} एक्सरसाइज सैं, अर सबतै पहलां {ex} करांगे।",
              "{day} का दिन सै भाई! {cnt} मूव सैं, शुरुआत {ex} तै।"],
    "warmup": ["पहलां थोड़ा वार्म-अप कर ले, हल्के सेट लगा ले, उनै मैं गिनूं कोनी। फेर असली सेट बता दीये।",
               "बिना गरमाए लोहा ना उठाइये भाई। हल्के सेट ले ले, फेर जब असली काम शुरू हो तो बता दीये।"],
    "react_easy": ["अरे वाह, यो तो हल्का था! {w} किलो पे {r} बार, बिल्कुल आराम तै।",
                   "घणा सौखा लाग्या तनै। अभी और दम सै तेरे में।",
                   "हल्का-फुल्का सेट था रै। वजन बढ़ाण की बारी आ गई।"],
    "react_good": ["शाबाश! {w} किलो पे {r} बार, एकदम सही जोर लाग्या।",
                   "यो हुई ना बात! इसे तरियां चलता रह।",
                   "बढ़िया सेट था भाई, जितना चाहिए था उतना ही जोर लाग्या।"],
    "react_grind": ["ओहो, यो तो घणा भारी था! पर तू डट्या रह्या, शाबाश।",
                    "इस सेट नै तो पसीने छुड़ा दिये! हिम्मत सै तेरी।",
                    "भारी लड़ाई थी रै, पर तू जीत गया।"],
    "react_miss": ["कोई बात नी भाई, यो सेट भारी पड़ गया। फिकर ना कर, फेर लगावांगे।",
                   "रेप्स छूट जाया करै सै, सबके साथ होवै। झाड़ दे अर अगला सेट पकड़।",
                   "ना-ना, घबरा मत। वजन थोड़ा घटावांगे अर फेर धमाल करांगे।"],
    "react_pr": ["ओहो! रिकॉर्ड तोड़ दिया रै! {pr_ex} में नया पीआर! शेर सै तू!",
                 "पीआर! तनै तो अपणा ही रिकॉर्ड पाड़ दिया! घणी बधाई भाई!",
                 "देखो रै इस नै! नया पर्सनल रिकॉर्ड! अखाड़े में नाम होगा तेरा!"],
    "inst_up": ["अगली बार {nw} किलो पे {nr} बार लगा, तू कर सकै सै।",
                "वजन बढ़ा दे, {nw} किलो अर {nr} बार।"],
    "inst_down": ["थोड़ा घटा ले, {nw} किलो पे {nr} बार, ताकि फॉर्म साफ रहवै।",
                  "{nw} किलो पे आ जा, {nr} बार, सही तरियां तै।"],
    "inst_same": ["इतने पे ही टिक्या रह, {nw} किलो पे {nr} बार।",
                  "फेर उतना ही, {nw} किलो पे {nr} बार। तू सेट सै।"],
    "rest_cue": ["{rest} आराम कर ले, घड़ी मैं देखूं सूं।",
                 "सांस ले भाई, {rest} बाद फेर भिड़ांगे।"],
    "ex_done": ["बस, {ex} पूरा! अब {nxt}, {nsets} सेट, {nreps} बार।",
                "{ex} तो निपटा दिया। अगला {nxt} सै।"],
    "ex_done_last": ["बस भाई, {ex} भी हो गया, आज का काम पूरा! जब तैयार हो तो 'खतम' बोल दीये, मैं हिसाब बता दयूं।",
                     "सारा काम हो गया रै! 'खतम' बोल दे, फेर पूरा हिसाब सुणाऊंगा।"],
    "rest_over": ["टैम पूरा! {ex}, कुल {tot} में तै सेट {n}। चाल भाई!",
                  "आराम खतम! {ex} का सेट {n}, कुल {tot}। उठ जा रै!"],
    "end": ["बस! आज का काम पूरा। {sets} सेट, {exn} एक्सरसाइज, कुल {tonnes} टन वजन उठाया। शेर सै तू!",
            "हो गया भाई! {sets} सेट, {exn} एक्सरसाइज अर {tonnes} टन लोहा। घणा बढ़िया काम करया।"],
    "end_pr": ["और सुण, आज {prs} बणाया तनै!",
               "अर हां, आज {prs} भी बणाया। याद राखिये!"],
    "end_prot": ["खाण-पीण का ध्यान राखिये: आज करीब {prot} ग्राम प्रोटीन और चाहिए। दूध, पनीर या अंडा ले लीये।"],
    "pain": ["रुक जा भाई! दर्द मैं जोर ना लगाइये। बता, कित दर्द हो रह्या सै?",
             "ठहर जा! दर्द सै तो वो मूव बंद कर। कुण-सी जगह दुखै सै, बता?"],
    "skip": ["ठीक सै, {ex} छोड़ दे। अगला {nxt} सै।",
             "कोई बात नी, {ex} रहण दे। चाल, {nxt} पकड़।"],
    "skip_last": ["{ex} छोड़ दिया। यो आखिरी था, 'खतम' बोल दे।"],
    "fit": ["ठीक सै भाई, {min} मिनट सैं तो {cnt} एक्सरसाइज अर {sets} सेट करांगे। जरूरी लोहा पक्का होगा।"],
    "status": ["तू {ex} पे सै, सेट {n}, कुल {tot} में तै। अभी {rem} सेट बाकी सैं।"],
    "fallback": ["ना समझ पाया भाई। एक्सरसाइज, वजन अर कितनी बार, इस तरियां बता: बेंच प्रेस अस्सी किलो आठ बार।",
                 "फेर बोल भाई। जैसे: स्क्वाट सौ किलो पांच बार।"],
    "ask_time": ["कित्ते मिनट सैं तेरे धोरे?"],
    "ready_go": ["चाल भाई! {ex}, सेट {n}, कुल {tot}।"],
    "sample": ["राम राम भाई! मैं सूं तेरा पहलवान कोच। पूरे सेशन में तेरे साथ रहूंगा, अर कमी ना छोड़ूंगा।"],
    "last_time": ["पिछली बार {ex} में {last} करया था तनै।"],
    "last_time_short": ["पिछली बार {last} करया था।"],
    "no_plan": ["पहलां अपणा प्लान बणा ले, फेर मेरे धोरे आइये।"],
    "already_done": ["आज का सारा काम तो हो चुक्या सै। 'खतम' बोल दे, मैं हिसाब दे दयूं।"],
    "resume": ["फेर आ गया? {done} सेट हो चुक्के सैं, अब {ex} का सेट {n} / {tot} सै।", "चाल भाई, {done} सेट होगे। {ex} का {n} नंबर सेट कर, कुल {tot} सैं।"],
    "nothing_logged": ["आज तो कुछ लिख्या कोनी, तो हिसाब कित का? जब लोहा उठा ले, फेर आइये।"],
    "rest_done": ["आराम पूरा। सारा काम निपट गया, 'खतम' बोल दे।"],
    "all_done": ["सारा काम हो गया सै भाई। हिसाब चाहिए तो 'खतम' बोल दे।"],
}

from backend.coach.lines_roast import ROAST  # noqa: E402  (Ustaad, the Hindi roaster)

BANKS["roast"] = ROAST

# Follow-up questions for half-described sets (offline fallback; with a model key the coach composes these live, see compose.py).
from backend.coach.lines_ask import ASK, ASK_SLOTS  # noqa: E402

SLOTS.update(ASK_SLOTS)
for _p, _d in ASK.items():
    BANKS[_p].update(_d)

# Skipping: confirm before skipping, put an exercise back, "what is next", a repeated set.
from backend.coach.lines_skip import SKIP, SKIP_SLOTS  # noqa: E402

SLOTS.update(SKIP_SLOTS)
for _p, _d in SKIP.items():
    BANKS[_p].update(_d)

HI_PERSONAS = {"haryanvi", "roast"}
HI_TOD = {"Morning": "सुबह", "Afternoon": "दोपहर", "Evening": "शाम"}
HI_REST = {"a minute": "एक मिनट", "about a minute": "करीब एक मिनट", "a minute and a half": "डेढ़ मिनट", "two minutes": "दो मिनट",
           "two and a half minutes": "ढाई मिनट", "three minutes": "तीन मिनट"}
HI_EX = {
    "bench press": "बेंच प्रेस", "squat": "स्क्वाट", "deadlift": "डेडलिफ्ट", "overhead press": "ओवरहेड प्रेस", "barbell row": "बारबेल रो",
    "lat pulldown": "लैट पुलडाउन", "pull up": "पुल अप", "curls": "कर्ल", "lunges": "लंजेस", "front squat": "फ्रंट स्क्वाट",
    "incline press": "इनक्लाइन प्रेस", "cable row": "केबल रो", "romanian deadlift": "रोमानियन डेडलिफ्ट", "leg curl": "लेग कर्ल",
    "calf raise": "काफ रेज", "lateral raise": "लेटरल रेज", "triceps pushdown": "ट्राइसेप्स पुशडाउन", "leg press": "लेग प्रेस",
    "face pull": "फेस पुल", "dips": "डिप्स", "hammer curls": "हैमर कर्ल", "plank": "प्लैंक", "push up": "पुश अप", "hip thrust": "हिप थ्रस्ट",
    "dumbbell press": "डंबल प्रेस", "dumbbell row": "डंबल रो", "leg extension": "लेग एक्सटेंशन", "goblet squat": "गॉब्लेट स्क्वाट",
}
HI_DAY = {
    "Full Body A": "फुल बॉडी ए", "Full Body B": "फुल बॉडी बी", "Full Body C": "फुल बॉडी सी", "Upper A": "अपर ए", "Upper B": "अपर बी",
    "Lower A": "लोअर ए", "Lower B": "लोअर बी", "Push": "पुश", "Pull": "पुल", "Legs": "लेग्स", "Upper": "अपर", "Lower": "लोअर",
}
_WEIGHT_REPS = re.compile(r"(\d+(?:\.\d+)?) for (\d+)")


def _localize(values: dict) -> dict:
    """Swap English slot values (exercise names, rest times, 'Morning', '80 for 8') for Devanagari so the Hindi voice reads them naturally."""
    out = dict(values)
    for k in ("ex", "nxt", "pr_ex"):
        if k in out:
            out[k] = HI_EX.get(str(out[k]).lower(), out[k])
    if "day" in out:
        out["day"] = HI_DAY.get(out["day"], out["day"])
    if "rest" in out:
        r = str(out["rest"])
        out["rest"] = HI_REST.get(r, r.replace(" seconds", " सेकंड"))
    if "tod" in out:
        out["tod"] = HI_TOD.get(out["tod"], out["tod"])
    if "last" in out:
        out["last"] = _WEIGHT_REPS.sub(lambda m: f"{m.group(1)} किलो पे {m.group(2)} बार", str(out["last"]))
    return out


def prs_phrase(persona: str, prs: list[str]) -> str:
    if persona in HI_PERSONAS:
        return f"{HI_EX.get(prs[0], prs[0])} में एक पीआर" if len(prs) == 1 else f"{len(prs)} पीआर"
    return f"a PR on {prs[0]}" if len(prs) == 1 else f"{len(prs)} PRs"


_last: dict[tuple, str] = {}
_recent: dict[tuple, list[str]] = {}
_rng = random.Random()


def seed(n: int | None) -> None:
    _rng.seed(n)


def tod(now: datetime | None = None) -> str:
    h = (now or datetime.now()).hour
    return "Morning" if h < 12 else "Afternoon" if h < 18 else "Evening"


def rest_text(sec: int) -> str:
    return {60: "a minute", 75: "about a minute", 90: "a minute and a half", 120: "two minutes",
            150: "two and a half minutes", 180: "three minutes"}.get(sec, f"{sec} seconds")


_ONE = re.compile(r"\b1 (exercise|set|lift|movement)s\b")
_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")


def tidy(text: str) -> str:
    """Spoken-text hygiene after slot filling: '1 exercises' -> '1 exercise', and capitalise sentence starts
    (a slot like an exercise name or '{rest}' can land at the start of a sentence)."""
    text = _ONE.sub(lambda m: "1 " + m.group(1), text)
    return _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def pick(persona: str, key: str, name: str = "", **ctx) -> str:
    """One phrasing, never the same as the previous one used for this persona+moment."""
    options = list(BANKS.get(persona, BANKS["hype"])[key])
    recent = _recent.setdefault((persona, key), [])
    pool = [o for o in options if o not in recent] or [o for o in options if o != (recent[-1] if recent else None)] or options
    line = _rng.choice(pool)
    recent.append(line)
    del recent[:-min(3, max(len(options) - 1, 1))]   # remember the last 3 (fewer when the bank is small) so the same line does not come back soon
    _last[(persona, key)] = line
    values = {"nm": f", {name}" if name else "", "tod": tod(), **ctx}
    if persona in HI_PERSONAS:
        values = _localize(values)
    return tidy(line.format(**values))
