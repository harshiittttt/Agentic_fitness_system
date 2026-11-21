"""Follow-up questions when a set is half-described ("1 set done" with no weight or reps): the coach asks for exactly what is missing.
Slots: {ex} exercise, {w} weight in kg, {r} reps. Every persona, several phrasings each (lines.pick never repeats the previous one)."""
from __future__ import annotations

ASK_SLOTS: dict[str, set[str]] = {
    "ask_both": {"ex"}, "ask_weight": {"ex", "r"}, "ask_reps": {"ex", "w"}, "ask_which": {"w", "r"},
    "ask_unclear": set(), "ask_cancel": set(),
}

ASK: dict[str, dict[str, list[str]]] = {
    "hype": {
        "ask_both": ["Nice, {ex} set in the books! What weight did you use, and how many reps?",
                     "Let's log it! Tell me the weight and the reps for that {ex} set.",
                     "Yes! Give me the numbers: how heavy, and how many reps on {ex}?",
                     "Love it. Quick, weight and reps for that {ex} set so I can count it."],
        "ask_weight": ["{r} reps on {ex}, nice! What weight was on the bar?", "Got {r} reps. How heavy was it?",
                       "{r} reps, solid. What was the weight?", "Okay, {r} reps on {ex}. Tell me the weight and I'll log it."],
        "ask_reps": ["{w} kilos on {ex}, got it. How many reps did you get?", "{w} kilos it is! How many reps?",
                     "Alright, {w} kilos on the bar. Reps?", "{w} kilos for how many reps on {ex}?"],
        "ask_which": ["{w} for {r}, got it. Which exercise was that?", "Nice, {w} for {r}! What lift was it?",
                      "I caught {w} for {r}, but which exercise?"],
        "ask_unclear": ["Didn't catch the numbers. Tell me like this: 80 for 8, weight then reps.",
                        "Say it again for me, weight and reps, like 60 for 10.",
                        "Hmm, I missed that. Weight and reps, like 80 for 8."],
        "ask_cancel": ["No problem, I dropped that one. Tell me when you're ready.", "All good, forgot it. Carry on.",
                       "Scrapped it. Next time, tell me the weight and reps."],
    },
    "mentor": {
        "ask_both": ["Good. What weight did you use, and how many reps on {ex}?",
                     "Before I log that {ex} set, tell me the weight and the reps.",
                     "Well done. How heavy was it, and how many reps did you get?",
                     "Let's get that recorded. Weight and reps for the {ex}, please."],
        "ask_weight": ["{r} reps on {ex}, good. What was the weight?", "Thank you. And the weight for those {r} reps?",
                       "I have {r} reps. How much was on the bar?", "{r} reps noted. What weight was it?"],
        "ask_reps": ["{w} kilos on {ex}. How many reps did you complete?", "I have {w} kilos. How many reps?",
                     "Good, {w} kilos. And the reps?", "{w} kilos noted. How many times did you lift it?"],
        "ask_which": ["{w} for {r}, noted. Which exercise was that?", "I have {w} for {r}. What was the exercise?",
                      "Which lift was {w} for {r}?"],
        "ask_unclear": ["I didn't quite get the numbers. Tell me the weight and then the reps, like 60 for 10.",
                        "Could you say it once more? Weight first, then reps, like 80 for 8.",
                        "I missed that. Try weight and reps together, like 70 for 6."],
        "ask_cancel": ["That's fine, I've dropped it. Tell me when you're ready.",
                       "No problem, forgotten. Carry on when you're ready.", "Understood, I won't log that one."],
    },
    "straight": {
        "ask_both": ["{ex} set. Weight and reps?", "Numbers for that {ex} set. Weight, reps.",
                     "Log it. Weight and reps on {ex}?", "What weight, how many reps?"],
        "ask_weight": ["{r} reps. Weight?", "{r} reps on {ex}. What weight?", "Got {r} reps. How heavy?", "Weight for those {r} reps?"],
        "ask_reps": ["{w} kilos. Reps?", "{w} on {ex}. How many reps?", "{w} kilos. How many reps?", "Reps at {w}?"],
        "ask_which": ["{w} for {r}. Which exercise?", "Which lift was {w} for {r}?", "{w} for {r}. What exercise?"],
        "ask_unclear": ["Didn't get the numbers. Weight then reps, like 80 for 8.", "Again. Weight, reps. Like 60 for 10.",
                        "Missed it. Weight and reps."],
        "ask_cancel": ["Dropped it.", "Forgotten. Carry on.", "Not logged. Next."],
    },
    "haryanvi": {
        "ask_both": ["शाबाश भाई! {ex} का सेट हो ग्या। कितणा वजन था अर कितनी बार करया, बता।",
                     "बढ़िया! अब बता, {ex} में वजन कितणा था अर कितनी बार उठाया?",
                     "सेट तो हो लिया, अब नंबर बता भाई। वजन कितणा अर बार कितनी?",
                     "{ex} का सेट लिख लूं? वजन अर बार, दोनों बता दे।"],
        "ask_weight": ["{r} बार हो लिए {ex} में, शाबाश! वजन कितणा था?", "{r} बार नोट कर लिए। वजन कितणा लगाया था भाई?",
                       "ठीक सै, {r} बार। अब वजन बता दे।", "{r} बार सुण लिए, वजन तो बता।"],
        "ask_reps": ["{w} किलो पे {ex}, ठीक सै। कितनी बार उठाया?", "{w} किलो लिख लिया भाई। बार कितनी करी?",
                     "बढ़िया, {w} किलो। अब कितनी बार किया, यो बता।", "{w} किलो पे कितनी बार?"],
        "ask_which": ["{w} किलो पे {r} बार, ठीक सै। पर कौन सी एक्सरसाइज थी भाई?", "नंबर सुण लिए, {w} किलो {r} बार। एक्सरसाइज कौन सी थी?",
                      "{w} पे {r} बार किसमें करया, बता दे।"],
        "ask_unclear": ["ना समझ पाया भाई। वजन अर बार, इस तरियां बता: अस्सी किलो आठ बार।",
                        "फेर बोल भाई। पहलम वजन, फेर बार। जैसे: साठ किलो दस बार।", "नंबर ना सुणे। एक बर फेर बता दे, वजन अर बार।"],
        "ask_cancel": ["ठीक सै भाई, छोड़ दिया। जब तैयार हो बता दिये।", "कोई बात नी, रहण दे। आगे चाल।", "चल छोड़, नोट ना करया।"],
    },
    "roast": {
        "ask_both": ["{ex} का सेट हो गया? शाबाश, अब बता भी दे, वजन कितना था और कितनी बार? या अंदाज़े से लिख दूँ?",
                     "सेट तो कर लिया, अब नंबर भी बोल। वजन कितना, बार कितनी? मैं अंतर्यामी नहीं हूँ।",
                     "{ex} हो गया बोल के निकल लेगा? वजन और बार, दोनों बता।",
                     "नंबर के बिना सेट का कोई मतलब नहीं। वजन कितना था, कितनी बार किया?"],
        "ask_weight": ["{r} बार, ठीक है। वजन बताएगा या रहस्य रखना है?", "{r} बार सुन लिए। अब वजन भी बोल दे, शरमा मत।",
                       "{r} बार किए {ex} में, मान लिया। वजन कितना था?", "वजन बता। {r} बार हल्के वजन पे भी हो जाते हैं।"],
        "ask_reps": ["{w} किलो पे {ex}, ठीक है। कितनी बार? गिनती तो आती होगी।", "{w} किलो लिख लिया। अब बार बता, मैं यहाँ बैठा गिन नहीं रहा।",
                     "{w} किलो, अच्छा। कितनी बार उठाया, सच बोलना।", "{w} किलो पे कितनी बार? ईमानदारी से।"],
        "ask_which": ["{w} किलो पे {r} बार, ठीक है। पर किसमें? एक्सरसाइज का नाम भी तो बोल।", "नंबर तो बता दिए, एक्सरसाइज कौन सी थी?",
                      "{w} पे {r} बार, कौन सी वाली? मैं मन नहीं पढ़ता।"],
        "ask_unclear": ["समझ नहीं आया। पहले वजन, फिर बार। जैसे: अस्सी किलो आठ बार। इतना भी नहीं होता?",
                        "फिर से बोल, ढंग से। वजन और बार, जैसे: साठ किलो दस बार।", "नंबर साफ नहीं आए। एक बार और, वजन फिर बार।"],
        "ask_cancel": ["ठीक है, छोड़ दिया। वैसे भी कोई बड़ी बात नहीं थी।", "रहने दे, नोट नहीं किया। तैयार हो तब बोल।",
                       "छोड़ दिया। अगली बार पूरे नंबर के साथ आना।"],
    },
}


# Bodyweight exercises (pull up, dips...): there is no weight to ask for, only reps.
ASK_SLOTS["ask_reps_only"] = {"ex"}
_REPS_ONLY = {
    "hype": ["{ex} set done, nice! How many reps did you get?", "Love it. How many reps on {ex}?", "Got it! Reps on that {ex} set?"],
    "mentor": ["Good. How many reps of {ex} did you complete?", "Well done. And the reps on {ex}?", "How many reps did you get on that {ex} set?"],
    "straight": ["{ex}. Reps?", "How many reps on {ex}?", "Reps?"],
    "haryanvi": ["{ex} का सेट हो ग्या भाई! कितनी बार करया?", "शाबाश! {ex} में कितनी बार किया, बता।", "{ex} कितनी बार हुआ, बता दे।"],
    "roast": ["{ex} हो गया? कितनी बार हुआ, गिनती तो बता।", "{ex} में कितनी बार किया? सच बोलना।", "कितनी बार हुआ {ex}? बोल भी दे।"],
}
for _p, _v in _REPS_ONLY.items():
    ASK[_p]["ask_reps_only"] = _v
