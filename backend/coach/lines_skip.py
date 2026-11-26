"""Lines for skipping, putting an exercise back, 'what is next'. Every persona, two or more phrasings each.
Slots: {ex} the exercise (or exercises), {nxt} the exercise that is current / next."""
from __future__ import annotations

SKIP_SLOTS: dict[str, set[str]] = {
    "skip_confirm": {"ex"}, "unskip": {"ex", "nxt"}, "unskip_none": set(), "next_is": {"ex", "nxt"},
}

SKIP: dict[str, dict[str, list[str]]] = {
    "hype": {
        "skip_confirm": ["Hold up, do you want to skip {ex}? Say yes and I'll drop it, or no and we keep going.",
                         "Wait, skip {ex}? Say yes to skip it, or no to keep it."],
        "unskip": ["{ex} is back on the list! You're up with {nxt}.", "Done, {ex} is back in. Next up is {nxt}."],
        "unskip_none": ["Nothing is skipped, we're doing the whole session!", "Nothing's been skipped, so everything is still on."],
        "next_is": ["You're on {ex} now, and {nxt} comes right after it.", "{ex} first, then {nxt} is next."],
    },
    "mentor": {
        "skip_confirm": ["Just to be sure, would you like to skip {ex}? Say yes to skip it, or no to keep it.",
                         "Do you want to skip {ex}? Tell me yes or no."],
        "unskip": ["Good. {ex} is back in the session, and you're on {nxt}.", "Understood, {ex} is restored. Next is {nxt}."],
        "unskip_none": ["Nothing has been skipped, the whole session is still on.", "You haven't skipped anything, so the plan is intact."],
        "next_is": ["You're on {ex} now. After that comes {nxt}.", "{ex} is current, and {nxt} follows it."],
    },
    "straight": {
        "skip_confirm": ["Skip {ex}? Yes or no.", "Do you want to skip {ex}? Say yes or no."],
        "unskip": ["{ex} is back. You're on {nxt}.", "Restored {ex}. Now {nxt}."],
        "unskip_none": ["Nothing is skipped.", "No skips today. The plan stands."],
        "next_is": ["Now {ex}. Then {nxt}.", "{ex} now, {nxt} next."],
    },
    "haryanvi": {
        "skip_confirm": ["रुक भाई, {ex} छोड़णा चाहवै सै? हां बोल तो छोड़ दयूं, ना बोल तो करांगे।",
                         "{ex} छोड़ दे? हां या ना बता दे।"],
        "unskip": ["चल ठीक सै, {ex} वापस जोड़ दिया। अब {nxt} सै।", "{ex} फेर आ गया सूची में। अगला {nxt} सै।"],
        "unskip_none": ["कुछ भी कोनी छोड़्या, सारा सेशन पूरा सै।", "तनै कुछ ना छोड़्या भाई, सारा काम बाकी सै।"],
        "next_is": ["अभी {ex} सै, उसके बाद {nxt} आवैगा।", "पहलां {ex}, फेर {nxt}।"],
    },
    "roast": {
        "skip_confirm": ["{ex} छोड़ना है? पक्का? हां बोल या ना बोल।", "अबे {ex} छोड़ेगा? हां या ना, साफ बोल।"],
        "unskip": ["चल ठीक है, {ex} वापस आ गया। अब {nxt} कर।", "{ex} फिर जुड़ गया। अगला {nxt} है, चल।"],
        "unskip_none": ["कुछ भी नहीं छोड़ा, पूरा सेशन बाकी है। बहाने मत ढूंढ।", "अभी तक कुछ नहीं छोड़ा तूने, सब कुछ बाकी है।"],
        "next_is": ["अभी {ex} कर, उसके बाद {nxt} आएगा।", "पहले {ex}, फिर {nxt}। जल्दी मत कर।"],
    },
}
