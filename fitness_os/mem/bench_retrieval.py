"""Labelled retrieval benchmark for the memory layer.

A synthetic athlete (about 170 live beliefs, 6 discoveries, superseded and denied beliefs, a small entity graph) and 70 labelled queries:
paraphrases with no word overlap, abbreviations, Hinglish / Hindi, negation and denied facts, superseded facts (temporal, current and as-of),
entity-linked queries, multi-hop (knee -> squat -> leg press) and distractors. Metrics per ranker: recall@1, recall@5, MRR, nDCG@5, plus the
number of FORBIDDEN items (superseded / denied beliefs) found in the top 5 (must be 0).

    python -m fitness_os.mem.bench_retrieval              offline hash embeddings (deterministic), on a scratch database
    python -m fitness_os.mem.bench_retrieval --real       the real Gemini embeddings (needs a key; one-off, never run by the tests)
    python -m fitness_os.mem.bench_retrieval --tune       also grid/random-search the fusion weights on 2/3 of the queries, report the held-out 1/3

It always works in a temporary database file; the real fitness.db is never touched.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import tempfile
from contextlib import contextmanager
from typing import Any, Iterator

NOW = "2026-06-15T12:00:00+00:00"
K = 10

# ============================================================================ corpus ============================================================================
# (key, predicate, object, body, options). Options: src (source), kind, from (valid_from), sens, deny (the athlete denied it afterwards).
def F(key: str, pred: str, obj: str, body: str, **o: Any) -> tuple:
    return (key, pred, obj, body, o)


FACTS: list[tuple] = [
    # ---- health and risk
    F("knee_squat", "injury_site", "left knee deep squats", "Your left knee aches when you squat below parallel."),
    F("shoulder_ohp", "pain_aggravated_by", "overhead press", "Your right shoulder pinches during overhead pressing."),
    F("back_dl", "injury_site", "lower back", "You strained your lower back two years ago and it flares up with heavy deadlifts."),
    F("wrist_rack", "pain_aggravated_by", "front rack", "Your wrists hurt in the front rack position, so you use a cross-arm grip."),
    F("allergy_peanut", "allergy", "peanuts", "You are allergic to peanuts."),
    F("lactose", "medical_condition", "lactose intolerance", "You are lactose intolerant, so milk upsets your stomach."),
    F("asthma", "medical_condition", "exercise-induced asthma", "You have mild exercise-induced asthma and carry an inhaler on runs."),
    F("ankle", "injury_site", "right ankle", "You sprained your right ankle last spring and it still feels unstable on uneven ground."),
    F("elbow", "pain_aggravated_by", "high-volume curls", "Your elbows get tender after high-volume curls and skull crushers."),
    F("neck", "pain_aggravated_by", "long desk days", "Your neck gets stiff after long days at the desk."),
    F("knee_relief", "pain_relieved_by", "foam rolling quads", "Foam rolling your quads for five minutes eases the knee ache."),
    F("hip_tight", "pain_relieved_by", "warm-up", "Your hips feel tight in the morning until you warm up for ten minutes."),
    F("avoid_jump", "avoids_movement", "box jumps", "You avoid box jumps because of the ankle."),
    # ---- exercise preferences
    F("likes_legpress", "likes_exercise", "leg press", "You like leg press and usually do four sets of twelve."),
    F("likes_dl", "likes_exercise", "deadlift", "You enjoy heavy deadlifts; they are your favourite lift."),
    F("likes_pullup", "likes_exercise", "pull-ups", "You like pull-ups and want to reach ten strict reps."),
    F("likes_row", "likes_exercise", "seated cable rows", "You like seated cable rows."),
    F("likes_swing", "likes_exercise", "kettlebell swings", "You enjoy kettlebell swings as a finisher."),
    F("likes_thrust", "likes_exercise", "hip thrusts", "You like hip thrusts and feel them in your glutes."),
    F("dislikes_lunge", "dislikes_exercise", "lunges", "You dislike lunges because they hurt your knees."),
    F("dislikes_burpee", "dislikes_exercise", "burpees", "You hate burpees."),
    F("dislikes_tread", "dislikes_exercise", "treadmill running", "You find treadmill running boring and avoid it."),
    F("dislikes_dips", "dislikes_exercise", "bench dips", "You dislike bench dips; they bother your shoulders."),
    F("likes_incline", "likes_exercise", "incline dumbbell press", "Incline dumbbell press feels comfortable and pain free for you."),
    F("likes_landmine", "likes_exercise", "landmine press", "You like landmine presses as a shoulder friendly option."),
    # ---- food
    F("diet_meat", "diet_type", "omnivore", "You eat everything including chicken and fish.", **{"from": "2026-01-05T00:00:00+00:00"}),
    F("diet_veg", "diet_type", "vegetarian", "You are vegetarian and do not eat meat or fish.", **{"from": "2026-04-01T00:00:00+00:00"}),
    F("likes_paneer", "likes_food", "paneer", "You like paneer and eat it most days for protein."),
    F("likes_dal", "likes_food", "dal rice", "You like dal and rice for dinner."),
    F("likes_oats", "likes_food", "oats", "You eat oats with banana for breakfast."),
    F("dislikes_mushroom", "dislikes_food", "mushrooms", "You dislike mushrooms."),
    F("dislikes_bittergourd", "dislikes_food", "bitter gourd", "You cannot stand bitter gourd."),
    F("whey", "supplement", "whey protein", "You take one scoop of whey protein after training."),
    F("creatine", "supplement", "creatine", "You take five grams of creatine every day."),
    F("skip_breakfast", "meal_pattern", "skips breakfast", "You often skip breakfast on workdays and eat a big lunch."),
    F("late_dinner", "meal_pattern", "late dinner", "You eat dinner late, around ten at night, which hurts your sleep."),
    F("chai", "meal_pattern", "tea", "You drink two cups of milky tea with sugar every day."),
    F("caffeine", "meal_pattern", "coffee", "You drink coffee before training but never after three in the afternoon."),
    F("hydration", "meal_pattern", "water", "You rarely drink enough water and feel thirsty by evening."),
    F("avoids_pork", "dislikes_food", "pork", "You never eat pork."),
    # ---- schedule and life
    F("time_evening", "prefers_training_time", "evening", "You used to train in the evening after work.", **{"from": "2026-01-05T00:00:00+00:00"}),
    F("time_morning", "prefers_training_time", "morning", "You prefer training in the morning around six.", **{"from": "2026-04-01T00:00:00+00:00"}),
    F("days3", "trains_days_per_week", "3", "You train three days a week.", **{"from": "2026-01-05T00:00:00+00:00"}),
    F("days4", "trains_days_per_week", "4", "You train four days a week.", **{"from": "2026-04-01T00:00:00+00:00"}),
    F("sess45", "prefers_session_minutes", "45", "You prefer short forty five minute sessions.", **{"from": "2026-01-05T00:00:00+00:00"}),
    F("sess60", "prefers_session_minutes", "60", "You like sessions of about sixty minutes.", **{"from": "2026-04-01T00:00:00+00:00"}),
    F("work_student", "work_pattern", "student", "You are a student with flexible afternoons.", **{"from": "2026-01-05T00:00:00+00:00"}),
    F("work_desk", "work_pattern", "desk job", "You work a desk job from nine to six.", **{"from": "2026-02-10T00:00:00+00:00"}),
    F("travel", "travel_pattern", "monthly work travel", "You travel for work about once a month and miss the gym that week."),
    F("sleep_sched", "sleep_schedule", "11pm to 6:30", "You go to bed around eleven and wake at half past six."),
    F("weekend_long", "weekend_pattern", "sunday hike", "You do a long hike or run on Sunday mornings."),
    F("busy_thu", "adherence_pattern", "thursday meetings", "Thursdays are packed with meetings so you usually skip training that day."),
    F("body_weight", "body_stat", "81 kg", "Your bodyweight is about 81 kilograms.", sens="body"),
    F("body_height", "body_stat", "178 cm", "You are 178 centimetres tall.", sens="body"),
    F("rhr", "body_stat", "58 bpm", "Your resting heart rate is around 58 beats per minute.", sens="body"),
    # ---- goals and motivation
    F("goal_old", "goal", "build muscle", "Your goal is to build muscle and gain size.", **{"from": "2026-01-05T00:00:00+00:00"}),
    F("goal_new", "goal", "lose fat", "Your goal is to lose fat while keeping your strength.", **{"from": "2026-03-20T00:00:00+00:00"}),
    F("tw_old", "target_weight_kg", "82", "You want to reach 82 kilograms.", **{"from": "2026-01-10T00:00:00+00:00"}),
    F("tw_new", "target_weight_kg", "78", "You want to reach 78 kilograms.", **{"from": "2026-03-20T00:00:00+00:00"}),
    F("event", "target_date", "2026-10-18", "You are training for a half marathon on 18 October."),
    F("mot_streak", "motivation", "streaks", "Seeing a training streak keeps you motivated."),
    F("mot_photos", "motivation", "progress photos", "Monthly progress photos motivate you."),
    F("mot_wedding", "motivation", "sister wedding", "You want to look good for your sister's wedding in December."),
    # ---- behaviour
    F("tone_tough", "responds_to_tone", "direct", "You respond better to direct, no-nonsense coaching than to gentle encouragement."),
    F("ignores_late", "ignores_nudges_at", "after 9 pm", "You ignore notifications that arrive after nine at night."),
    F("monday_dip", "adherence_pattern", "mondays", "You skip workouts more often on Mondays.", src="derived"),
    F("rarely_sat", "adherence_pattern", "saturdays", "You rarely train on Saturdays.", src="derived"),
    F("overplans", "adherence_pattern", "overplanning", "You plan more sessions than you finish after a stressful week.", src="derived"),
    # ---- episodes and milestones
    F("pr_dl", "milestone", "deadlift 140", "You pulled a 140 kg deadlift on 3 May, a new personal record.", kind="episodic"),
    F("pr_squat", "milestone", "squat 110", "You hit a 110 kg squat for five reps in March.", kind="episodic"),
    F("flu", "setback", "flu in April", "You missed a week of training in April because of the flu.", kind="episodic"),
    F("trip", "life_event", "Mumbai trip", "You spent a week in Mumbai in March with no gym access.", kind="episodic"),
    F("newjob", "life_event", "new job", "You started a new job in February, which cut your free time.", kind="episodic"),
    F("first10k", "milestone", "10k run", "You ran your first ten kilometres in 58 minutes in May.", kind="episodic"),
    F("rolled", "setback", "ankle roll", "You rolled your ankle playing football last June.", kind="episodic"),
    F("deload", "milestone", "deload week", "You took a deload week at the end of May and felt fresh afterwards.", kind="episodic"),
    # ---- equipment
    F("eq_home", "available_equipment", "dumbbells", "At home you only have dumbbells up to 24 kilograms and resistance bands."),
    F("eq_gym", "available_equipment", "commercial gym", "You have a commercial gym membership with full barbells and cable machines."),
    F("eq_nobar", "available_equipment", "no barbell", "You have no barbell or squat rack at home."),
    F("eq_kb", "available_equipment", "kettlebell", "You own a single sixteen kilogram kettlebell."),
    # ---- more training, sleep, stress, recovery
    F("ppl", "training_split", "push pull legs", "Your program is a push, pull, legs split."),
    F("warmup", "routine", "cycling warm-up", "You warm up with ten minutes of easy cycling before lifting."),
    F("rest_between", "routine", "rest", "You rest two to three minutes between heavy sets."),
    F("music", "preference_note", "hip hop", "You like training with loud hip hop music."),
    F("alone", "preference_note", "train alone", "You train alone and prefer it that way."),
    F("partner_raj", "life_event", "Raj", "Your training partner Raj joins you on Saturdays.", kind="episodic"),
    F("cool_room", "sleep_note", "cool room", "You sleep better when your bedroom is cool."),
    F("tea_night", "sleep_note", "tea after six", "You wake up at night if you drink tea after six in the evening."),
    F("nap", "sleep_note", "sunday nap", "You nap for twenty minutes on Sundays."),
    F("exam", "life_event", "work crunch", "Quarter end at work makes you drop training for two weeks.", kind="episodic"),
    F("anxious", "motivation", "pre-lift nerves", "You feel anxious before heavy singles.", sens="mental"),
    F("massage", "routine", "sports massage", "You get a sports massage once a month."),
    F("no_stretch", "routine", "stretching", "You do not stretch unless someone reminds you."),
    F("cold_shower", "routine", "cold shower", "A cold shower helps you feel awake after morning sessions."),
    # ---- everyday life (neutral neighbours)
    F("notebook", "routine", "log book", "You track your workouts in a notebook."),
    F("flat_shoes", "preference_note", "flat shoes", "You prefer flat shoes for lifting."),
    F("gym_near", "preference_note", "gym distance", "Your gym is a ten minute walk from home."),
    F("podcast", "preference_note", "podcasts", "You listen to podcasts on your commute."),
    F("swim", "likes_exercise", "swimming", "You like swimming on rest days."),
    F("sweet_tooth", "meal_pattern", "sweets", "You have a sweet tooth in the evenings."),
    F("eat_out", "meal_pattern", "eating out", "You eat out twice a week."),
    F("meal_prep", "meal_pattern", "meal prep", "You prepare your lunches in advance for the whole week."),
    F("watch", "preference_note", "heart rate watch", "You use a heart rate watch to track your runs."),
    F("groupchat", "motivation", "friends chat", "You share your progress in a friends group chat."),
    F("crowded", "preference_note", "crowds", "You find the gym too crowded at six in the evening."),
    F("mileage", "routine", "15 km a week", "You run about fifteen kilometres a week."),
    F("commute", "work_note", "metro", "Your commute is thirty minutes by metro."),
    F("pillow", "sleep_note", "pillow", "You sleep with two pillows."),
    F("screen", "sleep_note", "phone in bed", "You scroll on your phone in bed for half an hour."),
    F("sunlight", "routine", "sunlight", "You get a short walk in the sun after lunch."),
    # ---- Hindi / Hinglish beliefs
    F("h_dahi", "likes_food", "dahi sabzi", "Aapko roti aur sabzi ke saath dahi pasand hai."),
    F("h_tikka", "likes_food", "paneer tikka", "आपको पनीर टिक्का बहुत पसंद है।"),
    F("h_kamar", "injury_site", "kamar", "आपकी कमर में दर्द रहता है जब आप भारी वज़न उठाते हैं।"),
    F("h_late", "sleep_note", "der se sona", "Aap raat ko der se sote ho, aksar baarah baje ke baad."),
    F("h_doodh", "medical_condition", "doodh", "आपको दूध नहीं पचता।"),
    F("h_walk", "preference_note", "evening walk", "Aapko shaam ko sair karna pasand hai."),
    F("h_knee", "injury_site", "ghutne sujan", "Aapke ghutne mein sujan aa jaati hai long runs ke baad."),
    F("h_cricket", "preference_note", "cricket", "Aap weekend par family ke saath cricket khelte ho."),
]
# fillers: plenty of near-neighbours that must not win
_LIKED_FOODS = ["bananas", "almonds", "sweet potatoes", "chickpeas", "greek yogurt", "rajma", "poha", "idli", "smoothies", "dark chocolate", "apples", "lemon water"]
_DISLIKED_FOODS = ["okra", "olives", "pickles", "cabbage"]
_LIKED_MOVES = ["cable flies", "lat pulldowns", "farmer carries", "planks", "sled pushes", "face pulls", "calf raises", "leg curls"]
for _f in _LIKED_FOODS:
    FACTS.append(F(f"food_{_f}", "likes_food", _f, f"You enjoy {_f}."))
for _f in _DISLIKED_FOODS:
    FACTS.append(F(f"nofood_{_f}", "dislikes_food", _f, f"You dislike {_f}."))
for _f in _LIKED_MOVES:
    FACTS.append(F(f"move_{_f}", "likes_exercise", _f, f"You like {_f}."))
_DAYS = [("2026-05-04", "pull day", 62, 7.5), ("2026-05-06", "leg day", 55, 6.0), ("2026-05-08", "push day", 58, 7.0), ("2026-05-11", "pull day", 60, 8.0),
         ("2026-05-13", "leg day", 50, 5.5), ("2026-05-15", "push day", 64, 7.5), ("2026-05-18", "pull day", 59, 6.5), ("2026-05-20", "leg day", 57, 7.0),
         ("2026-05-22", "push day", 61, 7.2), ("2026-05-25", "pull day", 63, 8.1), ("2026-05-27", "leg day", 49, 5.9), ("2026-05-29", "push day", 60, 7.4),
         ("2026-06-01", "pull day", 58, 6.8), ("2026-06-03", "leg day", 56, 7.7)]
for _d, _t, _m, _s in _DAYS:
    FACTS.append(F(f"ep_{_d}", "happened", f"{_d} {_t}", f"On {_d} you trained {_t} for {_m} minutes after sleeping {_s} hours.", kind="episodic", **{"from": _d + "T08:00:00+00:00"}))

DENIED: list[tuple] = [
    F("D_burpee", "likes_exercise", "burpees", "You love burpees."),
    F("D_vegan", "diet_type", "vegan", "You are vegan."),
    F("D_dl", "dislikes_exercise", "deadlifts", "You hate deadlifts."),
    F("D_shellfish", "allergy", "shellfish", "You are allergic to shellfish."),
    F("D_rknee", "injury_site", "right knee", "Your right knee is injured."),
]
DISCOVERIES: list[tuple[str, str, str, str]] = [
    ("d_sleep", "sleep_performance", "Sleep drives your squat", "On days after you sleep 7.5 hours or more, your squat is about 4% stronger (31 sessions)."),
    ("d_monday", "adherence_weekday", "Monday adherence dip", "You complete only 55% of planned Monday sessions versus 82% on other days."),
    ("d_rpe", "rpe_bias", "You under-report effort", "On heavy sets you rate RPE about one point lower than your bar speed suggests."),
    ("d_maint", "maintenance", "Your real maintenance calories", "Your weight trend says your maintenance is about 2,650 kcal, roughly 300 above the formula."),
    ("d_volume", "dose_response", "Your chest volume sweet spot", "Your bench progress is best at 12 to 16 weekly sets; beyond 18 sets it stalls."),
    ("d_protein", "protein_recovery", "Protein and next-day recovery", "On days you eat 130 g protein or more, next-day soreness is about 0.6 points lower."),
]
# explicit graph edges, as the ingest step would write them: (src exercise/body part, relation, dst)
EDGES: list[tuple[str, str, str, str, str]] = [
    ("exercise", "squat", "alternative_to", "exercise", "leg press"),
    ("exercise", "overhead press", "alternative_to", "exercise", "landmine press"),
    ("exercise", "overhead press", "alternative_to", "exercise", "incline dumbbell press"),
    ("exercise", "deadlift", "alternative_to", "exercise", "hip thrust"),
]
ENTITIES = [("exercise", "landmine press", ["landmine presses"]), ("exercise", "incline dumbbell press", ["incline press"]), ("exercise", "hip thrust", ["hip thrusts"]),
            ("exercise", "leg press", []), ("exercise", "squat", ["squats", "squatting"]), ("body_part", "knee", ["knees", "ghutna", "ghutne"]),
            ("body_part", "shoulder", ["shoulders", "kandha", "kandhe"]), ("body_part", "back", ["kamar", "lower back"])]

# ============================================================================ queries ============================================================================
# (id, category, text, relevant keys, forbidden keys, as_of or None). Relevant = any of these counts as a hit; recall = share of them found in the top k.
def Q(cat: str, text: str, rel: list[str], forbid: list[str] | None = None, as_of: str | None = None) -> tuple:
    return (cat, text, rel, forbid or [], as_of)


FEB, FEB20 = "2026-02-15T12:00:00+00:00", "2026-02-20T12:00:00+00:00"
QUERIES: list[tuple] = [
    # paraphrases: no word overlap with the belief
    Q("paraphrase", "which movement makes my joint above the shin complain", ["knee_squat", "h_knee"]),
    Q("paraphrase", "foods that upset my stomach", ["lactose", "h_doodh"]),
    Q("paraphrase", "what time of day do I like to lift", ["time_morning"], ["time_evening"]),
    Q("paraphrase", "what keeps me going to the gym", ["mot_streak", "mot_photos", "mot_wedding"]),
    Q("paraphrase", "how do I perform after a short night", ["d_sleep"]),
    Q("paraphrase", "do I eat animal flesh", ["diet_veg"], ["diet_meat"]),
    Q("paraphrase", "which cardio machine bores me", ["dislikes_tread"]),
    Q("paraphrase", "what gear do I own at my apartment", ["eq_home", "eq_nobar", "eq_kb"]),
    Q("paraphrase", "why can I not have lattes", ["lactose", "h_doodh"]),
    Q("paraphrase", "what race am I preparing for", ["event"]),
    Q("paraphrase", "how heavy am I", ["body_weight"]),
    Q("paraphrase", "who works out alongside me", ["partner_raj", "alone"]),
    Q("paraphrase", "what do I do to loosen up before lifting", ["warmup", "hip_tight"]),
    Q("paraphrase", "am I allergic to anything", ["allergy_peanut"], ["D_shellfish"]),
    Q("paraphrase", "what supplement do I drink after exercise", ["whey"]),
    # abbreviations
    Q("abbrev", "ohp shoulder pain", ["shoulder_ohp"]),
    Q("abbrev", "DL PR", ["pr_dl"]),
    Q("abbrev", "rdl or conventional given my back history", ["back_dl", "h_kamar"]),
    Q("abbrev", "TDEE estimate", ["d_maint"]),
    Q("abbrev", "RPE bias", ["d_rpe"]),
    Q("abbrev", "PPL split", ["ppl"]),
    Q("abbrev", "bw and height", ["body_weight", "body_height"]),
    Q("abbrev", "resting HR", ["rhr"]),
    Q("abbrev", "BP weekly sets sweet spot", ["d_volume"]),
    # Hinglish / Hindi
    Q("hinglish", "mere ghutne mein dard kab hota hai", ["knee_squat", "h_knee"]),
    Q("hinglish", "kamar dard bhari weight", ["back_dl", "h_kamar"]),
    Q("hinglish", "main subah workout karta hoon", ["time_morning"], ["time_evening"]),
    Q("hinglish", "paneer pasand hai kya", ["likes_paneer", "h_tikka"]),
    Q("hinglish", "दूध पीने से problem", ["lactose", "h_doodh"]),
    Q("hinglish", "neend kam ho to taakat", ["d_sleep"]),
    Q("hinglish", "kandhe mein dard press karte waqt", ["shoulder_ohp"]),
    Q("hinglish", "ghar par kaunsa equipment hai", ["eq_home", "eq_nobar", "eq_kb"]),
    Q("hinglish", "mujhe kaunsa khana pasand nahi", ["dislikes_mushroom", "dislikes_bittergourd", "avoids_pork"]),
    Q("hinglish", "रात को देर से सोना", ["h_late", "late_dinner"]),
    Q("hinglish", "cricket kab khelta hoon", ["h_cricket"]),
    # negation and denied facts
    Q("negation", "burpees", ["dislikes_burpee"], ["D_burpee"]),
    Q("negation", "am I vegan", ["diet_veg"], ["D_vegan"]),
    Q("negation", "do I enjoy deadlifts", ["likes_dl"], ["D_dl"]),
    Q("negation", "shellfish allergy", [], ["D_shellfish"]),
    Q("negation", "right knee injury", [], ["D_rknee"]),
    Q("negation", "which exercises do I refuse to do", ["dislikes_lunge", "dislikes_burpee", "dislikes_tread", "dislikes_dips"]),
    Q("negation", "what do I never eat", ["avoids_pork", "diet_veg"]),
    # temporal: current, then the past
    Q("temporal", "what is my goal", ["goal_new"], ["goal_old"]),
    Q("temporal", "how many days per week do I train", ["days4"], ["days3"]),
    Q("temporal", "how long are my sessions", ["sess60"], ["sess45"]),
    Q("temporal", "when do I train", ["time_morning"], ["time_evening"]),
    Q("temporal", "what do I eat, meat or not", ["diet_veg"], ["diet_meat"]),
    Q("temporal", "target body weight", ["tw_new"], ["tw_old"]),
    Q("temporal", "what is my job", ["work_desk"], ["work_student"]),
    Q("asof", "what was my goal", ["goal_old"], ["goal_new"], FEB),
    Q("asof", "how many days did I train", ["days3"], ["days4"], FEB),
    Q("asof", "when did I train", ["time_evening"], ["time_morning"], FEB),
    Q("asof", "what was my diet", ["diet_meat"], ["diet_veg"], FEB),
    Q("asof", "target body weight", ["tw_old"], ["tw_new"], FEB20),
    Q("asof", "what was my goal back in January", ["goal_old"], ["goal_new"], "auto"),
    # entity-linked
    Q("entity", "squat", ["knee_squat", "d_sleep", "pr_squat"]),
    Q("entity", "deadlift", ["likes_dl", "back_dl", "pr_dl"]),
    Q("entity", "knee", ["knee_squat", "h_knee", "knee_relief", "dislikes_lunge"]),
    Q("entity", "shoulder", ["shoulder_ohp", "dislikes_dips", "likes_landmine"]),
    Q("entity", "paneer", ["likes_paneer", "h_tikka"]),
    # multi-hop through the graph
    Q("multihop", "what can replace squats because of my knee", ["likes_legpress"]),
    Q("multihop", "shoulder friendly alternative to overhead press", ["likes_landmine", "likes_incline"]),
    Q("multihop", "back safe alternative to deadlift", ["likes_thrust"]),
    Q("multihop", "knee problem so no squats, what lower body lift do I like", ["likes_legpress"]),
    # distractors (look-alikes that must not win)
    Q("distractor", "right shoulder", ["shoulder_ohp"]),
    Q("distractor", "left knee", ["knee_squat"]),
    Q("distractor", "coffee timing", ["caffeine"]),
    Q("distractor", "weekend training", ["weekend_long", "rarely_sat"]),
    Q("distractor", "tea", ["chai", "tea_night"]),
    Q("distractor", "dinner", ["late_dinner", "likes_dal"]),
]


# ============================================================================ build ============================================================================
@contextmanager
def scratch_db() -> Iterator[str]:
    """Point the backend at a temporary database for the duration (the real fitness.db is never opened)."""
    from backend.memory import db
    old_path, old_now = db.DB_PATH, os.environ.get("FITNESS_NOW")
    with tempfile.TemporaryDirectory() as d:
        db.DB_PATH = os.path.join(d, "bench.db")
        os.environ["FITNESS_NOW"] = NOW
        try:
            yield db.DB_PATH
        finally:
            db.DB_PATH = old_path
            if old_now is None:
                os.environ.pop("FITNESS_NOW", None)
            else:
                os.environ["FITNESS_NOW"] = old_now


def build_corpus() -> dict[tuple[str, int], str]:
    """Write the synthetic athlete into the (scratch) store. -> {(item_type, id): key}."""
    import importlib
    from . import embed, store
    consolidate = importlib.import_module("fitness_os.mem.consolidate")     # `mem.consolidate` is also the facade function, so import by name
    ids: dict[tuple[str, int], str] = {}

    def put(key: str, pred: str, obj: str, body: str, o: dict) -> None:
        r = store.add_fact(pred, obj, body=body, source=o.get("src", "stated"), kind=o.get("kind", "semantic"), sensitivity=o.get("sens"),
                           valid_from=o.get("from") or "2025-11-01T00:00:00+00:00")
        ids[("fact", r["id"])] = key

    for key, pred, obj, body, o in DENIED:      # first, so a denied single-valued fact (vegan) cannot supersede a real one
        put(key, pred, obj, body, o)
        fid = next(i for (t, i), k in ids.items() if k == key)
        store.user_deny(fid, "bench: the athlete said this is wrong")
    for key, pred, obj, body, o in FACTS:
        put(key, pred, obj, body, o)
    for key, family, title, statement in DISCOVERIES:
        d = store.upsert_discovery(key, family, title, statement, {"confidence": 0.9, "n": 30}, status="confirmed")
        ids[("discovery", d["id"])] = key
    for kind, name, aliases in ENTITIES:
        store.entity(kind, name, aliases)
    consolidate.run()                       # entities + co-mention edges from fact text, embeddings
    for ka, a, rel, kb, b in EDGES:
        store.add_edge(store.entity(ka, a), rel, store.entity(kb, b))
    embed.reindex()
    return ids


# ============================================================================ metrics ============================================================================
def _metrics(ranked: list[str], rel: list[str]) -> dict[str, float]:
    rs = set(rel)
    first = next((i for i, k in enumerate(ranked, 1) if k in rs), None)
    dcg = sum(1.0 / math.log2(i + 1) for i, k in enumerate(ranked[:5], 1) if k in rs)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rs), 5) + 1))
    return {"r1": len(rs & set(ranked[:1])) / len(rs), "r5": len(rs & set(ranked[:5])) / len(rs), "mrr": 1.0 / first if first else 0.0, "ndcg5": dcg / ideal if ideal else 0.0,
            "hit5": 1.0 if rs & set(ranked[:5]) else 0.0}


def run_queries(ids: dict, queries: list[tuple], weights: dict | None, per_query: bool = False) -> dict[str, Any]:
    from . import retrieve
    rows, violations, ranked_all = [], 0, []
    for cat, text, rel, forbid, as_of in queries:
        ao = retrieve.parse_as_of(text) if as_of == "auto" else as_of
        hits = retrieve.search(text, k=K, as_of=ao, weights=weights)
        ranked = [ids.get((h.item_type, h.item_id), f"?{h.item_type}{h.item_id}") for h in hits]
        ranked_all.append(ranked)
        violations += len(set(forbid) & set(ranked[:5]))
        if rel:
            rows.append({"cat": cat, **_metrics(ranked, rel)})
    agg = {m: sum(r[m] for r in rows) / len(rows) for m in ("r1", "r5", "mrr", "ndcg5", "hit5")} if rows else {}
    by_cat: dict[str, dict] = {}
    for c in sorted({r["cat"] for r in rows}):
        sub = [r for r in rows if r["cat"] == c]
        by_cat[c] = {m: sum(r[m] for r in sub) / len(sub) for m in ("r5", "mrr")} | {"n": len(sub)}
    return {**agg, "violations": violations, "by_cat": by_cat, "n": len(rows)}


BM25_ONLY = {"bm25": 1.0, "dense": 0.0, "graph": 0.0, "recency": 0.0, "conf": 0.0, "salience": 0.0, "pinned": 0.0, "mmr": 1.0}
DENSE_ONLY = {"bm25": 0.0, "dense": 1.0, "graph": 0.0, "recency": 0.0, "conf": 0.0, "salience": 0.0, "pinned": 0.0, "mmr": 1.0}
NO_GRAPH = {"graph": 0.0}


def split(queries: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """Deterministic 2/3 train, 1/3 held-out test, stratified by category."""
    train, test, seen = [], [], {}
    for q in queries:
        n = seen.get(q[0], 0)
        seen[q[0]] = n + 1
        (test if n % 3 == 2 else train).append(q)
    return train, test


def table(results: dict[str, dict]) -> str:
    out = [f"{'ranker':<22}{'recall@1':>9}{'recall@5':>9}{'MRR':>8}{'nDCG@5':>8}{'forbidden@5':>13}"]
    for name, r in results.items():
        out.append(f"{name:<22}{r['r1']:>9.3f}{r['r5']:>9.3f}{r['mrr']:>8.3f}{r['ndcg5']:>8.3f}{r['violations']:>13d}")
    return "\n".join(out)


def tune(ids: dict, train: list[tuple], trials: int = 60, seed: int = 7) -> tuple[dict, float]:
    rnd = random.Random(seed)
    from . import retrieve
    best, best_s = dict(retrieve.WEIGHTS), -1.0
    base = run_queries(ids, train, None)
    best_s = 0.5 * base["mrr"] + 0.5 * base["r5"]
    for _ in range(trials):
        w = {"bm25": rnd.choice([0.7, 1.0, 1.3]), "dense": rnd.choice([0.7, 1.0, 1.3, 1.6]), "graph": rnd.choice([0.0, 0.3, 0.6, 1.0]), "conf": rnd.choice([0.0, 0.2, 0.4]),
             "recency": rnd.choice([0.0, 0.12, 0.25]), "mmr": rnd.choice([0.7, 0.85, 1.0]), "fusion": rnd.choice(["linear", "linear", "rrf"]), "rrf_k": rnd.choice([4.0, 8.0, 20.0])}
        r = run_queries(ids, train, w)
        s = 0.5 * r["mrr"] + 0.5 * r["r5"] - 0.5 * r["violations"]
        if s > best_s:
            best, best_s = {**retrieve.WEIGHTS, **w}, s
    return best, best_s


def _load_cache(path: str | None) -> None:
    """Optional on-disk cache of real embeddings (benchmark only: a re-run then costs no API quota)."""
    import numpy as np
    from . import embed
    if path and os.path.exists(path):
        z = np.load(path, allow_pickle=False)
        for k in z.files:
            m, h = k.split("|", 1)
            embed._cache[(m, h)] = z[k]


def _save_cache(path: str | None) -> None:
    import numpy as np
    from . import embed
    if path and embed._cache:
        np.savez(path, **{f"{m}|{h}": v for (m, h), v in embed._cache.items()})


def evaluate(real: bool = False, do_tune: bool = False, verbose: bool = True, cache: str | None = None) -> dict[str, Any]:
    from . import embed, retrieve
    embed.set_offline(not real)
    _load_cache(cache)
    with scratch_db(), embed.patient():
        ids = build_corpus()
        model = embed.model_name()
        out: dict[str, Any] = {"model": model, "n_beliefs": sum(1 for (t, _), k in ids.items() if t == "fact" and not k.startswith("D_")), "n_queries": len(QUERIES)}
        rankers = {"BM25 only": BM25_ONLY, "dense only": DENSE_ONLY, "hybrid (no graph)": NO_GRAPH, "hybrid": None}
        res = {name: run_queries(ids, QUERIES, w) for name, w in rankers.items()}
        out["all"] = res
        train, test = split(QUERIES)
        out["train"] = {name: run_queries(ids, train, w) for name, w in rankers.items()}
        out["test"] = {name: run_queries(ids, test, w) for name, w in rankers.items()}
        if do_tune:
            best, s = tune(ids, train)
            out["tuned_weights"] = best
            out["tuned_train"] = run_queries(ids, train, best)
            out["tuned_test"] = run_queries(ids, test, best)
            out["default_test"] = out["test"]["hybrid"]
        if verbose:
            print(f"embedding model: {model}; {out['n_beliefs']} live beliefs, {len(DISCOVERIES)} discoveries, {len(QUERIES)} queries ({len(test)} held out)")
            print("\nALL QUERIES\n" + table(res))
            print("\nHELD-OUT THIRD\n" + table(out["test"]))
            print("\nhybrid by category (recall@5 / MRR)")
            for c, v in res["hybrid"]["by_cat"].items():
                b, d = res["BM25 only"]["by_cat"].get(c, {}), res["dense only"]["by_cat"].get(c, {})
                print(f"  {c:<11} n={v['n']:<3} hybrid {v['r5']:.2f}/{v['mrr']:.2f}   bm25 {b.get('r5', 0):.2f}/{b.get('mrr', 0):.2f}   dense {d.get('r5', 0):.2f}/{d.get('mrr', 0):.2f}")
            if do_tune:
                print("\nTUNED weights:", {k: out["tuned_weights"][k] for k in ("bm25", "dense", "graph", "conf", "recency", "mmr", "fusion", "rrf_k")})
                print("train", {k: round(out["tuned_train"][k], 3) for k in ("r5", "mrr")}, "| held-out tuned", {k: round(out["tuned_test"][k], 3) for k in ("r5", "mrr")},
                      "vs default", {k: round(out["default_test"][k], 3) for k in ("r5", "mrr")})
        _save_cache(cache)
        return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="use the real embedding model (needs a key)")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--cache", default=None, help="npz file caching the real embeddings between runs")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    evaluate(real=a.real, do_tune=a.tune, cache=a.cache)


if __name__ == "__main__":
    main()
