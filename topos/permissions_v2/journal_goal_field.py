"""The structured goal field (IF-5, Lane H1): a journal goal grounded by the owner's own typed goal field.

A time-log journal entry carries the owner's typed ``goal`` field twice: ``build_time_log_content`` renders it as the
entry's first paragraph ("Goal: <text>") and the journal mapper stores it as ``metadata_json.goal``. On the measured
node every entry that carries one (80) matches. For the journal family only, a goal whose text IS that field,
verbatim, is grounded: the owner typed it into a goal field, so no model and no paraphrase stand between the
owner's words and the item (JOURNAL_TYPED_ITEMS_MEASUREMENT.md, "Recommendation"; WS0's spec for Lane H1).

Grounding is all this module decides. Everything else still holds where it always held (``knowledge_projections``):
the entry's owner proof, posture, the NSFW hard withhold, owner-only, exclusions and Off-limits over every column,
its assessment and the grant's decision, the window, and a derived goal's lineage revision. What this rule relies
on it re-checks at the point of use (guard independence): the entry's NSFW flag, the owner's authorship of the
entry's words and the attested self, and Off-limits on the goal text.

``refusal`` returns the first guard that withholds, as a code (never text), or None when the field grounds the goal:

- ``goal_field_disabled``: ``TOPOS_PERMISSIONS_V2_JOURNAL_GOAL_FIELD`` is off (the default).
- ``goal_field_nsfw``: the entry is NSFW-flagged (its column, or a flag in its metadata).
- ``goal_field_absent`` / ``goal_field_mismatch``: the entry carries no goal field; or its rendered first paragraph
  and ``metadata_json.goal`` differ (an edited or re-synced row); or the goal is not the field, verbatim.
- ``goal_field_author``: the entry is not the owner's own original wording, or there is no attested self.
- ``goal_field_special_category``: the entry's own assessed sensitivity is neither none nor personal.
- then the goal text itself (``text_refusal``), in this order: its shape and characters, Off-limits, special
  categories, a question or quote, reported speech, negation, hedges, sarcasm, an ended state, a deferral to an
  indefinite future (``not_yet``: a goal is future by nature, so OD-38's fact-only future guard cannot apply to it
  as it stands), a third party, a word the rule has not vetted, and anything that is not an intention.

The text guards are closed lists, and every uncertainty withholds:

- an intention opens with a task verb from ``TASK_VERBS``, after an optional time word and first-person prefix
  ("Today I'll ...", "My goal is to ...", or the verb alone). A list title, a URL, a placeholder, a motto, a pasted
  quote, a question or a second clause (any punctuation but a word's own hyphen or apostrophe and one final stop) is
  not;
- a special category is a word, its OD-38 stem, a root inside a word, a medical or drug ending, or a phrase from
  the lists below, and OD-38's ``SPECIAL`` is reused unchanged;
- a third party is OD-38's ``THIRD_PARTY`` vocabulary and these roles and relations, a possessive other than a
  time's, a capitalised word that is not a month, a day, a known acronym or a known tool or language, a person by
  trade ("-ologist", "-ician"), a verb whose object is a person ("remind", "thank"), the object of a contact verb
  ("call", "text", "help") that is not a known non-person, and any name the node itself holds for someone other
  than the owner (``known_people``) or the entry names in its people field;
- every word must be one the rule has vetted (``VETTED`` and the other lists, a number or code, or a contraction
  of one). Indirect special categories, people and foreign words are mostly words no list has vetted, and no
  closed list of sensitive words can be complete: this is what keeps the rule closed (measured on synthetic probe
  sets written for it, see the CHANGELOG entry).

The lists hold no personal names. They over-withhold on purpose: a plain goal with a word the vocabulary lacks is
withheld, never released.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

from . import entailment_grounding as eg
from .entity_boundary import normalized

FLAG = "TOPOS_PERMISSIONS_V2_JOURNAL_GOAL_FIELD"
VERSION = "journal-goal-field/v1"
PREFIX = "Goal: "                 # build_time_log_content's rendering of the field
PARAGRAPH = "\n\n"                # ... and its paragraph separator
# A "Goal:" line, read after entity_boundary.normalized (case folded, compatibility forms and invisible characters
# read through): optional leading punctuation, list or quote marks, then "goal" and a colon.
_GOAL_LINE = re.compile(r"^[\W_]*goal\s*:", re.MULTILINE)
MAX_WORDS = 40


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(FLAG, "")).strip().lower() == "true"


def _words(text: str) -> frozenset:
    return frozenset(text.split())


def _phrases(text: str) -> tuple:
    return tuple(tuple(part.split()) for part in text.replace("\n", " ").split("|") if part.strip())


# --- the field -----------------------------------------------------------------------------------

def _metadata(entry: dict):
    """The entry's metadata object, {} when it has none, or None when it is not a JSON object."""
    raw = entry.get("metadata_json")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def field_state(entry) -> tuple[str | None, str | None]:
    """(the structured goal field, None), or (None, why there is none).

    The field is ``metadata_json.goal``, and only while the entry's text renders exactly it as its first paragraph:
    "Goal: " + the field, then the end of the text or a blank line. A field the text does not render, a "Goal:"
    paragraph the metadata does not hold, the two differing (an edited or re-synced row), a further "Goal:" line
    anywhere after the first paragraph or in any other column or metadata value, or a metadata document naming
    "goal" twice is a mismatch.
    """
    if not isinstance(entry, dict):
        return None, "goal_field_absent"
    content = entry.get("content") if isinstance(entry.get("content"), str) else ""
    metadata = _metadata(entry)
    stored = metadata.get("goal") if isinstance(metadata, dict) else None
    stored = stored if isinstance(stored, str) and stored else None
    if stored is None and metadata is not None and not content.startswith(PREFIX):
        return None, "goal_field_absent"
    if stored is None or not (content == PREFIX + stored or content.startswith(PREFIX + stored + PARAGRAPH)):
        return None, "goal_field_mismatch"
    # The text states the goal once: a further "Goal:" line anywhere after the first paragraph (a second Goal
    # paragraph, an edited or re-synced entry), in the people column or any other text column, or in a metadata
    # value other than the goal itself is a mismatch too, in any case, width or invisible spelling; so is a metadata
    # document that holds "goal" twice (a last-wins parser sees one value, the document holds two).
    others = [content[len(PREFIX + stored):]]
    others += [value for key, value in entry.items() if key not in ("content", "metadata_json") and isinstance(value, str)]
    others += list(_texts_besides_goal(metadata))
    if any(_GOAL_LINE.search(normalized(text)) for text in others) or _goal_key_repeated(entry):
        return None, "goal_field_mismatch"
    return stored, None


def _texts_besides_goal(value, depth=0):
    """Every key and string in a metadata object except the top-level goal's own value."""
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            if not (depth == 0 and key == "goal"):
                yield from _texts_besides_goal(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _texts_besides_goal(item, depth + 1)
    elif isinstance(value, str):
        yield value


def _goal_key_repeated(entry) -> bool:
    """Whether the metadata document's top-level object names "goal" more than once."""
    raw = entry.get("metadata_json")
    if not isinstance(raw, str) or not raw:
        return False
    counts = []
    def pairs(items):
        counts.append(sum(1 for key, _ in items if key == "goal"))
        return dict(items)
    try:
        json.loads(raw, object_pairs_hook=pairs)
    except ValueError:
        return False
    return bool(counts) and counts[-1] > 1


def structured_field(entry) -> str | None:
    """The entry's structured goal field, or None (see ``field_state``)."""
    return field_state(entry)[0]


def _nsfw(entry: dict) -> bool:
    from topos.disclosure.content_policy import is_record_nsfw
    try:
        if is_record_nsfw(entry):
            return True
        metadata = _metadata(entry) or {}
        return any(is_record_nsfw({"content_nsfw": metadata.get(key)}) for key in ("nsfw", "content_nsfw", "is_nsfw"))
    except Exception:  # noqa: BLE001 -- a flag that cannot be read withholds
        return True


def refusal(goal_text, entry, *, boundary, author_is_owner: bool, subject_attested: bool, sensitivity: str,
            people=frozenset(), env=None) -> str | None:
    """Why the structured goal field does not ground ``goal_text`` on this journal ``entry`` (a code), or None.

    ``entry``: the cited journal row as qualification loaded it (``content``, ``metadata_json``, ``content_nsfw``,
    ``people``).
    ``author_is_owner``: the entry's qualified labels say owner-authored original wording (``author_of``).
    ``subject_attested``: the owner has exactly one attested self (OD-29), the goal's subject.
    ``sensitivity``: the entry's qualified sensitivity label; anything but none or personal withholds here too.
    ``people``: the node's own people (``known_people``): a goal naming one, in any case, is a third party's.
    ``boundary``: an object with ``mentions_protected(*texts)`` (``EntityBoundary``); None withholds.
    """
    if not enabled(env):
        return "goal_field_disabled"
    if not isinstance(entry, dict):
        return "goal_field_absent"
    if _nsfw(entry):
        return "goal_field_nsfw"
    field, code = field_state(entry)
    if code is not None:
        return code
    if type(goal_text) is not str or goal_text != field:
        return "goal_field_mismatch"
    if author_is_owner is not True or subject_attested is not True:
        return "goal_field_author"
    if sensitivity not in ("none", "personal"):
        return "goal_field_special_category"
    return text_refusal(field, boundary=boundary, people=frozenset(people) | _names_in(entry.get("people")))


def _names_in(value) -> frozenset:
    """The name words of a free-text people field ("Ana, Leo"): the people the entry itself says were there."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(_plain(word) for word in _TOKEN.findall(_fold(value)) if len(word) >= 2) - _COMMON


def known_people(conn) -> frozenset:
    """The node's own people, as name words: every word of a person entity's name and aliases and of a contact's
    display name, the owner's own self excepted, less the rule's own ordinary words. A table the node does not
    have adds nothing; a store that cannot be read raises, and the caller withholds."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    names = []
    if "entities" in tables:
        for name, aliases in conn.execute("SELECT canonical_name, aliases_json FROM entities "
                                          "WHERE entity_type='person' AND COALESCE(is_self, 0)=0"):
            names.append(name)
            try:
                decoded = json.loads(aliases) if isinstance(aliases, str) and aliases else []
            except ValueError:
                decoded = []
            if isinstance(decoded, list):
                names.extend(alias for alias in decoded if isinstance(alias, str))
    if "contacts" in tables:
        names.extend(name for (name,) in conn.execute("SELECT display_name FROM contacts WHERE COALESCE(is_self, 0)=0"))
    return frozenset().union(*(_names_in(name) for name in names))


# --- vocabularies --------------------------------------------------------------------------------
# Each list holds ordinary words only: never a person's name.

_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
_QUOTES = frozenset('"`\u201c\u201d\u00ab\u00bb\u2018\u201e\u2039\u203a')

# The verbs an intention may open with: the base form of a task. A gerund, a past tense, a noun, and a state, motto
# or consumption verb (be, stay, keep, live, love, feel, believe, trust, let, eat, drink, smoke) are absent on purpose.
TASK_VERBS = _words("""
add address adjust analyse analyze annotate answer apply approve archive arrange assemble attend audit automate back
bake balance batch benchmark bike blog boil book brainstorm brew build buy calculate call cancel capture carve catch
caulk change charge chart check choose chop clean clear climb close code collect commit compile complete compose
compost configure confirm connect consolidate contact continue convert cook copy correct count cover create crochet
crop cut cycle debug declutter decorate define delete deliver demo deploy deposit design develop digitise digitize
disinfect do document download draft draw drill drive drop dry dust edit email embroider empty enrol enroll estimate
evaluate exchange exercise expand explore export extend ferment figure file fill film finalise finalize find finish
firm fix flesh fold format frame fry gather generate get glue go grab grade grill grind handle hang harvest help hem
hike hit hoover host implement import improve install insulate integrate investigate iron jog join jot kayak kick
knit knock label launch lead learn lift line load lock log mail maintain make map marinate mark measure meet
memorise memorize mend merge message migrate mock model monitor mop mount move mow mulch nail name note onboard open
optimise optimize order organise organize outline pack paddle paint patch pay pick pickle pin pitch plan plant play
plot polish post practice practise prep prepare present price print prioritise prioritize process produce profile
program proofread prototype prune publish purchase push put quilt rake read rearrange rebase reboot rebuild
reconcile record recycle redesign redo reduce refactor refine register rehearse reinstall release remove rename
render renew reorganise reorganize repaint repair replace reply reread reschedule research reserve reset resize
resolve respond restart restock restore restructure resume return review revise rewrite ride rinse roast roll row
run sand sanitise sanitize save scan schedule scope scrub sculpt sell send serve set sew ship shop shovel sign
simplify skate sketch ski skim sleep solve sort sow spin split squash stack stain start steam stock store stretch
structure study submit summarise summarize surf sweep swim sync tackle take tally tape teach test text tidy tile
track train transcribe translate trim troubleshoot tune type uninstall unpack unsubscribe update upgrade upload
vacuum validate varnish verify visit volunteer wake walk wash watch water wax weed whittle winterise winterize wipe
work wrap write
""")
# A word that may stand before the verb and change nothing about it ("deep clean", "quickly review").
PRE_VERB = _words("deep quickly properly fully carefully thoroughly")
# A first-person intention before the verb (casefolded token sequences); the subject may be dropped ("Will ...").
FIRST_PERSON = _phrases("""
i want to|i wanna|i will|i'll|i am going to|i'm going to|i am gonna|i'm gonna|i plan to|i intend to|i aim to|
i need to|i have to|i must|i am determined to|i'm determined to|i am committed to|i'm committed to|my goal is to|
my plan is to|my aim is to|my intention is to|will|want to|need to|goal is to|plan is to
""")
# A time word that may open the field ("Today I'll ..."; a time followed by a colon is a list title and refused).
LEADING_TIME = _phrases("today|tonight|tomorrow|this morning|this afternoon|this evening|this week|this weekend")
TIME_WORDS = _words("""
today tonight tomorrow yesterday morning afternoon evening night day days week weeks weekend month months quarter
quarters year years decade hour hours minute minutes monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november december
""")

# Special categories: OD-38's SPECIAL, extended for a goal typed in passing. A word, its OD-38 stem, a root inside a
# word, a medical or drug suffix, or a phrase. Health (physical, mental, reproductive, sexual; disability, addiction
# and recovery; medicines), religion and beliefs, sex life, orientation and gender identity, politics, trade
# unions, race and ethnicity, immigration status, criminal matters, genetic and biometric data, and money.
SPECIAL_HEALTH = _words("""
ache aches acne adderall addict addicted addiction advil afib alcohol alcoholic allergies alprazolam alzheimer
alzheimers ambien ambulance amputation amputee anaemia anaesthesia anemia anesthesia aneurysm angina angioplasty
ankle ankles anorexia antabuse antibiotic antibiotics antidepressant antidepressants antihistamine anxious apnea
apnoea appendix appointment appointments appt arrhythmia arthritis ativan audiologist benzo benzos binge biologic
biologics biopsy bladder bleeding blood bloodwork bmi bones booze bowel bowels brace braces braille breast
breastfeeding breasts bronchitis bulimia bupropion calorie calories cancers cannabis carbs carcinoma cataract
cataracts catheter cbd cbt celexa celiac cerebral cervical cervix cgm checkup chiropractor cholesterol chronic
cialis cigarette cigarettes cigs clinical clinics cocaine coeliac colitis colonoscopy concerta concussion condom
condoms constipation contraception contraceptive copay cortisone cough counsellor counselor covid cpap crohn crohns
crutches cymbalta cyst dbt deaf dental dentist depo dermatologist detox diagnose dialysis diarrhea diet dieting
dietitian dizziness dizzy donor donors dosage dose doses doula dr drinking drinks drug drugs drunk dyslexia
dysphoria ears eczema edibles effexor emdr emphysema endo endometriosis endoscopy epipen erectile escitalopram
estradiol estrogen eyes fainting fasting fatigue fentanyl fever fibroids fibromyalgia finasteride flu fluoxetine
fmla fracture fractured gastro gastroenterologist genital gerd glaucoma glucose gluten gp gums gyn gynaecologist
gynecologist gyno handicap handicapped hangover hearing hematoma hemorrhoids hepatitis hernia heroin herpes hives
hormone hormones hospice hospitalized hpv hrt hypertension ibs ibuprofen icu immunotherapy implant implants
incontinence infection infertility infusion inhaler injection injections insomnia intake iud iui juul ketamine keto
kidney kidneys klonopin knee knees lactation lancet lancets lexapro libido lithium liver lsd lump lumps lung lungs
lupus lymphoma lyrica mammogram mammograms mammography marijuana mastectomy mdma medicaid medical medicare medicine
meditate meditation melanoma melatonin menopause menstrual metformin meth methadone midwife migraine migraines
mindfulness minoxidil mole moles mounjaro mri mushrooms myeloma naloxone narcan narcolepsy nausea nebulizer
neurologist nexplanon nicotine norco nurse nutritionist ob obgyn ocd oncologist oncology opioid opioids optometrist
orthodontist orthopaedic orthopedic osteoporosis ostomy overdose ovulation oxycodone ozempic pacemaker pain
painkiller painkillers palliative palsy panic pap paralysis paralyzed paraplegic pathology patient patients paxil
pcos pcp pediatrician percocet perimenopause pharmacist pharmacy phentermine phq physician pill pills platelets pmdd
pms pneumonia podiatrist postpartum prednisone prenatal prescriptions progesterone prostate prosthesis prosthetic
prozac psilocybin psoriasis psych psychiatric psychosis psychotherapy ptsd quadriplegic radiologist rash recovery
referral refill refills reflux rehab relapse remission reproductive ritalin rosacea rx sarcoma saxenda scan
schizophrenia sciatica screening seizures semaglutide seroquel sertraline shrink shrooms sick sickness sinus smoke
smoking sober sperm spinal spine spironolactone sponsor ssdi ssi ssri ssris statin statins stds stent sterilization
steroid steroids stis stitches stomach strattera stroke suboxone suicidal suicide surgeon surgical swab swabs
syndrome tablets tamoxifen taper tbi telehealth testosterone thc thyroid tinnitus tobacco tonsils tooth toothache
tramadol transfusion transplant trauma treatment tremor truvada tumour tylenol ulcer ultrasound urologist uterus
vaccinated vaccination vaccine vaccines valium valtrex vape vaping vasectomy vax vegan vegetarian vertigo viagra
vicodin vitals vivitrol vomiting vyvanse weed wegovy wellbutrin wheelchair withdrawal wound xanax zepbound zoloft
zyn
""")
SPECIAL_BELIEF = _words("""
aarti abaya adhan advent agnostic allah altar amish anglican apostle ardas astrology atheism baisakhi baptise
baptised baptist baptize baptized benediction bhajan bible bibles biblical bimah bishop blessing blessings brahmin
bris buddha buddhism burqa catechism catechumen cathedral chabad chakra chakras challah chant chanting chapel
chaplain chauth christ christianity christmas chuppah churches communion compline confession convent coven covenant
crucifix crucifixion darshan daven davening deacon devotion devotional devotions dharma dhikr dianetics diocese
disciples divine diwali doxology dua duas easter eid ekadashi episcopal etrog eucharist evangelical evangelism fajr
fast gospel granth gurdwara gurpurab guru hadith haftarah hajj hanukkah havan havdalah heathen hijab hinduism holi
holy homily horoscope horoscopes hymn hymnal hymns iftar imbolc iqama isha islam islamic jain jainism janazah japa
japji jehovah jew judaism jummah jumuah kaaba kabbalah kaddish karwa kashrut ketubah khalsa khutbah kiddush kippah
kirpan kirtan kufi kwanzaa langar lauds lds lectionary lenten litany liturgy lord lulav lutheran madrasa madrassa
maghrib mandir mangalsutra mantra masjid matins matzah matzo mehndi mennonite menorah methodist metta mihrab mikvah
minister minyan missionary mitzvah monastery monk mormon mufti namaz nativity navratri nikah niqab novena nun
orthodox pagan parashah parsha passover penance pentecost pentecostal pilgrimage pooja pope prasad prasadam prayed
prayers preacher presbyterian prophet protestant psalm psalms puja purim qibla quaker qurbani rabbi rakhi raksha
rcia rebbe reiki religious repent repentance resurrection rosary rosh sabbat sabbath sacrament sadaqah sahur salah
salat samhain sangha satsang scapular scientology scripture scriptures seder sehri selichot seminary sesshin seva
sewa shabbat shabbos shavuot shia shofar shraddh shul siddur sikhism sindoor sinner sins smudge smudging stupa sufi
sukkah sukkot sunnah sunni surah sutra sutras swami tabernacle tafsir tallit talmud taraweeh tarawih tarot tasbih
tashlich tefillin teshuvah theology tilak tithe tithing turban tzedakah ummah umrah upanishad upanishads vaisakhi
vatican vedas vesak vespers vicar vigil vipassana walima watchtower wicca wiccan wudhu wudu yagna yahrzeit yarmulke
yeshiva yom yule zakat zazen zodiac
""")
SPECIAL_IDENTITY = _words("""
aboriginal aclu activism activists advocacy afl afro agender ancestral ancestry antifa aromantic arraignment arrests
asexual bail ballots bdsm bipoc biracial blm brexit campaign campaigning candidate candidates canvass canvassing
caucasian celibacy celibate chicana chicano cio closeted communism comrade comrades congress congressional
congressman congresswoman conservatives consulate convictions cops council courthouse courts crime crimes custody
daca deadname defendant defender delegate delegates democracy deposition detective diaspora dnc dsa dues ead
elections electoral embassy erotic expunge expunged expungement fascist feeld felon felonies felons fetish
fingerprints gender genderfluid genderqueer genetics genomic governor grievance grievances h1b heritage hispanic
hookups immigrants impeach impeachment indicted indictment indigenous inmate intersex judge juneteenth jury kinky
labour latina latine latino latinx leftist legislator legislature lgbtq lgbtqia liberals lobbying lobbyist lube
marxist masturbate masturbation midterm midterms minorities minority misdemeanour monogamy multiracial naacp
naturalisation naturalise naturalize nra offender okcupid onlyfans orgasm pansexual parliament parliamentary
paternity petition petitions phonebank phonebanking picketing plea poc police politician politicians polyamorous
polyamory porn pornography pride primaries progressive pronoun pronouns prosecutor protests racism racist rallies
rally referendum representative republic residency retina rnc seiu senate senator sentencing sexting socialism
solidarity stewards strikes subpoena suing superpac swinger teamsters testify testimony tories tory transitioning
transsexual tribal tribe uaw uscis verdict virgin virginity visas voters warrant warrants
""")
# Medicines (generic and brand; brand names that are also personal names are left out on purpose), conditions,
# procedures and the body parts a health goal names.
SPECIAL_MEDICINE = _words("""
acetaminophen acyclovir adalimumab advair albuterol alendronate aleve allopurinol amitriptyline amlodipine
amoxicillin amoxil amphetamine anastrozole apixaban apretude aripiprazole atenolol atorvastatin augmentin
azithromycin baclofen benadryl benzonatate biktarvy bisoprolol boniva brilinta budesonide bumetanide buprenorphine
buspar buspirone bystolic cabenuva carbamazepine carvedilol cefdinir cefuroxime celebrex celecoxib cephalexin
cetirizine chantix chlorthalidone cipro ciprofloxacin citalopram claritin clindamycin clonazepam clonidine
clopidogrel codeine colchicine coumadin crestor cyclobenzaprine dapagliflozin depakote descovy desvenlafaxine
dexamethasone dextroamphetamine diazepam diclofenac diflucan digoxin dilantin diltiazem diphenhydramine divalproex
donepezil dovato doxepin doxycycline duloxetine eliquis empagliflozin enalapril enbrel entresto epinephrine
esomeprazole eszopiclone ezetimibe famotidine farxiga fenofibrate flagyl flomax flonase fluconazole fluticasone
focalin furosemide gabapentin genvoya glimepiride glipizide glyburide guanfacine haloperidol humalog humira
hydralazine hydrochlorothiazide hydrocodone hydrocortisone hydroxychloroquine hydroxyzine imitrex invokana
ipratropium isosorbide januvia jardiance keflex keppra ketorolac kyleena labetalol lamictal lamotrigine lansoprazole
lantus lasix latanoprost latuda letrozole levaquin levetiracetam levofloxacin levothyroxine lidocaine linagliptin
lipitor liraglutide lisdexamfetamine lisinopril loestrin loratadine lorazepam losartan lovastatin lunesta meclizine
medroxyprogesterone meloxicam memantine methocarbamol methotrexate methylphenidate methylprednisolone metoclopramide
metoprolol metronidazole minocycline mirena mirtazapine mobic mometasone montelukast morphine motrin mucinex
mupirocin naltrexone namenda naproxen neurontin nexium nifedipine nitrofurantoin nitroglycerin norethindrone
nortriptyline norvasc novolog nuvaring nuvigil nystatin olanzapine olmesartan omeprazole ondansetron oseltamivir
oxcarbazepine oxybutynin pantoprazole paragard paroxetine penicillin pepcid phenazopyridine pioglitazone plavix
pradaxa pramipexole pravastatin prednisolone pregabalin premarin prilosec pristiq promethazine propranolol protonix
provigil quetiapine qulipta ramipril ranitidine remeron restasis rexulti risperdal risperidone rivaroxaban
rizatriptan robaxin ropinirole rosuvastatin rybelsus sildenafil simvastatin singulair sitagliptin skyrizi sotalol
spiriva spiro sprintec stelara sucralfate sulfamethoxazole sumatriptan symbicort synthroid tadalafil tamiflu
tamsulosin tegretol terazosin tirzepatide tizanidine topamax topiramate toradol torsemide tradjenta trazodone
trelegy triamcinolone trintellix triumeq trulicity ubrelvy valacyclovir valsartan venlafaxine verapamil victoza
warfarin xarelto xyzal zetia zithromax zocor zofran zolpidem zyprexa zyrtec
""")
SPECIAL_CONDITION = _words("""
acl amen angiogram apologetics asl beer beliefs binder binders blister blisters booster boycott bruise bruises
bunion casino chickenpox civics cocktail cocktails colon copd cope coping cramping cramps detention diagnoses
dissociate dissociation ecg echocardiogram eeg ekg elbow elbows electrocardiogram enby flashback flashbacks
gallbladder gambling gout hallelujah hip hips hotline iep immunization incense influenza intrusive jab jaw lama
lesion lesions ligament ligaments liquor lyme lymph mayor measles meltdown meltdowns meniscus molar molars mugshot
nightmares nodule nodules norovirus obese obesity ovaries ovary overstimulated overweight pac pancreas pew pews
polls polyp polyps pumping rectum reverend rotator rsv scalp sensory shoulder shoulders shrine sonogram spiritual
spirituality spleen splint sprain sprained stimming strep suhoor sutures teeth tendon tendons tequila testicle
testicles throat titer toe toes trimester underweight urinalysis urine uti utis verse verses vertebra vertebrae
visitation vodka whiskey wine wrist wrists xray
""")
# Money (Lane P4): debts, loans, savings, investments, income and budgets. Sets 3 and 5 held finance goals that no
# word here caught (the special-category guard fired on none of their seven finance cases), so a finance goal
# withholds like the categories above. Words, not roots: "invest" is inside "investigate", "crypto" inside
# "cryptography", and "bonds", "interest", "shares" and "bill" are ordinary goal words too.
SPECIAL_FINANCE = _words("""
debt debts indebted loan loans mortgage mortgages remortgage savings invest invests invested investing investment
investments investor investors salary salaries paycheck paychecks payday wage wages income incomes budget budgets
budgeting budgeted bankrupt bankruptcy overdraft overdrawn tax taxes taxed pension pensions retirement annuity
annuities dividend dividends stocks brokerage crypto cryptocurrency bitcoin ethereum refinance refinancing repay
repaying repayment repayments afford affordable lender lenders creditor creditors foreclosure insolvency alimony
frugal finances financial financially financing 401k roth
""")
SPECIAL_EXTRA = (SPECIAL_HEALTH | SPECIAL_BELIEF | SPECIAL_IDENTITY | SPECIAL_MEDICINE | SPECIAL_CONDITION
                 | SPECIAL_FINANCE)
# A root inside any word ("psychotherapist", "antidepressant", "prediabetic"). Each was read against the common
# words it also hits ("fertilizer", "hospitality", "psyched"): those withhold too, on purpose.
SPECIAL_ROOTS = tuple("""
therap psych oncolog chemo pharma medicat medicin medical prescri surger surgic symptom syndrom disorder diseas
cancer tumor tumour diabet insulin epilep seizur dementi alzheim parkinson arthrit autis adhd ptsd depress anxiet
suicid overdos addict alcohol rehab detox sobri pregnan fertil miscarr abortion contracep menstru menopaus ovulat
gynec gynaec obstet urolog prostat mammogra colonoscop endoscop biops vaccin hepatit chlamyd gonorr syphil dysphori
transgend lgbt homosex bisexu pansexu nonbinar religio church mosque synagog cathedr worship scriptur gospel quran
koran torah talmud theolog rosary ramadan shabbat sabbath passover hanukk chanuk mitzvah baptis baptiz catholic
christian muslim islam jewish judai hindu buddh sikh atheis politic democrat republican electoral ballot
caucus senator congressm congressw parliament unioniz unionis immigra citizenship naturaliz naturalis deport asylum
refugee undocumented arrest convict misdemean probation parole incarcer prison jail indict expung genetic genom
biometric fingerprint ethnic racial racis hospital clinic physician dentist orthodont pediatr paediatr
dermatolog cardiolog neurolog radiolog patholog nurse feminiz feminis thyroid endocrin rheumat hematolog nephrolog
pulmonolog gastroenter immunolog allerg anesthes anaesthes
""".split())
# Endings that name a condition, a procedure or a medicine (checked against an English word list: the common words
# that share one, such as "nostalgia", are exempt below).
SPECIAL_SUFFIXES = tuple("""
itis ectomy otomy ostomy oscopy plasty algia emia aemia phobia sartan olol statin prazole cillin mycin cycline
floxacin azepam zolam codone morphone glutide gliptin formin xetine traline talopram tiapine zapine peridone barbital
caine clovir navir tegravir fovir tinib parin farin dipine thiazide semide lukast olone triptan dronate tidine tadine
tirizine profen proxen fenac pramine triptyline zodone trigine methasone ticasone sonide trexate setron gabalin
pentin patide opril ipril zepril osis opathy
""".split())
SUFFIX_EXEMPT = _words("academia nostalgia dichotomy bohemia osmosis symbiosis metamorphosis mitosis meiosis "
                       "apotheosis")
SPECIAL_PHRASES = _phrases("""
green card|work permit|case status|blood work|blood test|lab work|lab results|labs drawn|my results|from the lab|
test strips|get tested|getting tested|got tested|get checked|getting checked|get screened|get scanned|get vaccinated|
get boosted|get jabbed|get seen|get treated|get swabbed|get examined|get looked at|get well|get better|
getting better|feel better|lose weight|losing weight|gain weight|my weight|weigh in|weigh myself|my period|my cycle|
period tracker|birth control|plan b|morning after|on prep|flu shot|booster shot|annual physical|physical exam|
eye exam|sleep study|breathing exercises|support group|to group|group therapy|my sponsor|days sober|days clean|
day clean|stay clean|staying clean|day chip|month chip|year chip|one day at a time|twelve step|12 step|al anon|
self harm|panic attack|anger management|name change|my transition|social transition|medical transition|chest binder|
puberty blockers|coming out|come out|drag show|drag queen|drag brunch|non binary|sunday service|sunday school|
hebrew school|youth group|bible study|mission trip|kingdom hall|meeting house|ash wednesday|good friday|
burn sage|town hall|city council|council meeting|phone bank|knock on doors|door knocking|organizing drive|
organising drive|union card|collective bargaining|ballot measure|yard sign|community service|public defender|
restraining order|ankle monitor|court date|drug test|spit kit|carrier screening|x ray|ct scan|pet scan|
clinical trial|urgent care|my levels|blood sugar|buy weed|smoke weed|intermittent fasting|planned parenthood|
egg freezing|c section|pap smear|the generic|permanent resident|first nations|i 130|i 485|i 765|i 20|n 400|i 94|
i 9|glp 1|group session|group sessions|home group|count my days|count days|the cast|my cast|cast off|my mood|
mood tracker|mood journal|crisis line|crisis plan|hot flashes|baby shower|two spirit|background check|my trial|
panic attacks|anxiety attack|change my name|my new name|my legal name|legal name|chosen name|
preferred name|my name on|pay off|paying off|paid off|pay down|paying down|credit card|credit cards|credit score|
emergency fund|net worth|spend less|spending less|side hustle|
make money|making money|earn more|cut spending|money goal|money goals|401 k
""")

# "smoke test" is a software test, not smoking; "scan", "weed" and "fast" are tasks in the verb's own slot.
SPECIAL_UNLESS_VERB = _words("weed fast scan smoke")

# Third parties: OD-38's THIRD_PARTY, extended with roles and relations (never a name).
THIRD_PARTY_EXTRA = _words("""
accountant adviser advisor advisors anybody attorney aunts babies babysitter bestie bf bff bosses boys bro brothers
buddies caregiver caretaker coaches cofounder cofounders colleagues contractor contractors cousins crew dads
daughters dudes editor editors electrician employee employees founder founders gang gentleman gentlemen gf girls
goddaughter godfather godmother godparent godson grandchild grandchildren granddaughter grandkid grandkids
grandparent grandparents grandson handyman household hubby husbands infant intern interns investor investors ladies
lady landlady let's lover managers mechanic men mentee mentees moms nanny nephews newborn nieces pals person persons
plumber pupil pupils recruiters relative relatives sis sisters sons squad staff stepbrother stepdad stepdaughter
stepfather stepmom stepmother stepsister stepson student students team teammate teammates teams tenant tenants
toddler toddlers twins uncles whoever wifey wives women
""")
# Words for a person by trade or field ("endocrinologist", "pediatrician", "photographer", "florist").
PERSON_SUFFIXES = ("ologist", "iatrist", "ician", "ographer", "ist", "ists")
PERSON_SUFFIX_EXEMPT = _words("list lists checklist checklists playlist playlists wishlist wishlists twist twists mist "
                              "fist gist exist exists assist assists insist persist resist consist enlist whist")
THIRD_PARTY_PHRASES = _phrases("date night|group chat|in laws|in law|cover for|fill in for|stand in for|sub for|"
                               "look after|care for|take care of|watch over|on behalf")
# Verbs whose object is almost always a person: a goal built on one is the owner acting on someone else.
PERSON_VERBS = _words("""
assign babysit befriend coach comfort congratulate console convince delegate encourage forgive hire hug interview
introduce invite kiss marry mentor nudge persuade remind tell thank tutor
""")
# Verbs that take a person as easily as a thing: in the verb's own slot, every word of its object must be a known
# non-person (a closed list), so "call the bank" stands while "call" or "help" with anyone or anything unlisted is
# a third party.
CONTACT_VERBS = _words("call phone ring email message text ping dm contact meet visit help ask reply respond answer "
                       "talk speak chat host support see")
PARTICLES = _words("up out back to with off")
SPAN_END = _words("about re regarding for at by before after from of in on to with and then into over during until "
                  "today tonight tomorrow")
SPAN_SKIP = _words("""
the a an my all some any every each this that these those few two three four five ten twenty new old next last first
second third final big small large little quick short long full whole entire main other extra early late fresh
clean dirty broken quarterly monthly weekly daily annual yearly remaining pending open overdue current upcoming simple
basic public private shared important urgent outstanding existing missing unused unread spare online offline remote
digital printed written same different
""")
NONPERSON = _words("""
agenda airline alert alerts application applications aquarium backlog bank banks branch budget bug bugs build cable
campus carrier changes city code college comment comments company concert concerts contract contracts count cv
dashboard deadline deadlines deck demo demos department deploy desk dmv docs document documentation documents draft
drafts effort email emails estimate event events exhibit exhibition farmers feedback file files film films form
forms forum gallery game games goal gym helpdesk hotel inbox incident incidents insurance insurer internet
invitations invites invoice invoices isp issue issues items launch lecture lectures library link links logs mail
market match meeting meetings meetup message messages metrics migration milestone milestones movie movies museum
newsletter notes notifications numbers office outage page pager pages park photos pictures plan portfolio post
presentation project projects proposal provider question questions queue quota receipt receipts release repo report
reports request requests restaurant resume retro review reviews rollout rsvp rsvps shop show shows site slides
spreadsheet standup store studio summary supplier suppliers support survey surveys sync target targets texts thread
threads ticket tickets university update updates upgrade utilities utility vendor vendors venue video videos
voicemail voicemails webinar webinars website wiki word words workshop zoo
""")

# Speech acts on the goal text, beyond OD-38's own lists.
HEDGES_EXTRA = _words("i'd try trying attempt attempting hoping ideally aspire aspiring potentially somewhat "
                      "roughly either")
SARCASM_EXTRA = _words("ha smh ugh sigh meh whatever yay lolol")
# An ended state in the goal text: OD-38's ENDED without the words a goal uses for its own outcome (finish, complete,
# done, "until" a time, "over"), plus "already".
ENDED_GOAL = _words("formerly former previously retired ended quit quitting abandoned was were had ex anymore past "
                    "stopped dropped gave given left already")
# Deferred to an indefinite future: an aspiration not yet taken up.
NOT_YET = _words("someday sometime eventually later oneday soonish")
NOT_YET_PHRASES = _phrases("""
one day|some day|at some point|in the future|down the road|one of these days|in a few years|when i have time|
when i get around|when i retire|after i retire|before i die|bucket list|next year|next decade|long term|some time|
in a while|at some stage|no rush
""")

# Not an intention.
PLACEHOLDERS = _words("tbd tba todo todos placeholder lorem ipsum asdf qwerty xxx xx untitled etc misc checklist")
PLACEHOLDER_PHRASES = _phrases("""
your goal|goal here|enter goal|enter a goal|type here|add a goal|set a goal|write your|describe your|example goal|
sample goal|test goal|test entry|goal 1|goal one|insert|today count
""")
# A two-word field ending in one of these is a list's title ("work stuff", "work tasks").
LIST_NOUNS = _words("list lists stuff things tasks todos items misc agenda priorities backlog checklist log dump")
# Right after the verb, these make a motto rather than a task ("work hard", "move fast").
MOTTO_NEXT = _words("hard harder smart smarter fast faster big bigger different differently forward early often twice "
                    "again it everything anything less more well better best free happy positive strong humble kind "
                    "calm")
# A motto's or a pasted quote's vocabulary, after the verb, and an object too vague to be a task.
VAGUE = _words("""
life world dream dreams soul universe journey happiness love peace gratitude kindness courage wisdom truth
purpose passion success greatness excellence fear fears moment present yourself matters possible impossible whatever
everything anything something what whoever wherever whenever growth mindset vibes energy spirit it thing things stuff
""")
QUESTION_START = _words("how what why when where who whom whose which whether should could would can does did is are "
                        "am was were shall may might")
SUBJECTS = _words("i we you they he she")

# The words a goal may be made of, beyond the lists above: a closed vocabulary of everyday task words, written for
# this rule, with no personal name and no word whose ordinary sense is a special category. A word outside every list
# withholds ("goal_field_unvetted_word"): an indirect special category, a person or a foreign word is usually a word
# the rule has not vetted. OD-38's stemmer reads an inflection ("chapters", "finishing") as its base.
VETTED = frozenset(word for line in """
# work, office, writing
report reports deck decks slide slides presentation presentations proposal proposals draft drafts doc docs document
documents documentation spec specs memo memos email emails inbox mail message messages note notes summary summaries
agenda outline outlines plan plans planning roadmap roadmaps strategy budget budgets forecast forecasts estimate
estimates invoice invoices receipt receipts expense expenses reimbursement reimbursements contract contracts
agreement agreements application applications form forms paperwork file files folder folders spreadsheet
spreadsheets sheet sheets table tables chart charts graph graphs dashboard dashboards metric metrics goal goals
target targets milestone milestones deadline deadlines task tasks ticket tickets issue issues bug bugs feature
features request requests review reviews feedback survey surveys meeting meetings sync syncs standup standups retro
retros sprint sprints backlog kanban board boards project projects launch launches release releases version versions
update updates changelog newsletter newsletters blog blogs post posts article articles essay essays paper papers
chapter chapters section sections page pages book books manuscript story stories script scripts thesis dissertation
research analysis analyses data dataset datasets model models experiment experiments result results findings insight
insights portfolio resume cover letter letters bio profile website websites site sites landing homepage copy content
headline headlines tagline brand branding logo logos design designs mockup mockups wireframe wireframes prototype
prototypes demo demos pitch pitches sales lead leads account accounts pipeline pipelines funnel marketing ad ads
announcement handoff handover writeup recap retrospective brief briefs overview pricing price prices offer offers
job jobs career course syllabus lesson lessons workshop workshops talk talks keynote webinar webinars conference
conferences offsite event events venue schedule schedules calendar calendars itinerary template templates guide
guides tutorial tutorials example examples sample samples revision revisions edit edits proofread copyedit
translation translations transcript transcripts caption captions subtitle subtitles footnotes bibliography citations
citation references reference index glossary abstract introduction intro conclusion conclusions methods discussion
figure figures diagram diagrams census permissions permission grants
# tech
code codebase repo repos branch branches commit commits merge merges pull push pr tests test testing unit units
integration endpoint endpoints api apis server servers database databases db query queries schema schemas migration
migrations indexes cache caching queue queues worker workers cron function functions class classes module modules
package packages library libraries dependency dependencies build builds deploy deployment deployments ci cd docker
container containers cluster clusters config configs configuration settings environment environments staging
production prod dev local logs logging monitoring alerts alerting latency performance memory leak leaks crash
crashes error errors exception exceptions timeout timeouts retry retries race condition conditions flaky parser
compiler interpreter linter lint formatter type types typing refactor refactoring cleanup debt tech technical flag
flags toggle toggles rollout rollback hotfix patch patches fix fixes upgrade upgrades readme wiki onboarding
component components screen screens view views layout layouts style styles stylesheet ui ux frontend backend
fullstack mobile app apps web browser extension extensions plugin plugins integrations webhook webhooks auth
authentication login logout signup password passwords token tokens key keys role roles user users billing payment
payments checkout cart order orders search ranking recommendation recommendations training inference prompt prompts
eval evals benchmark benchmarks accuracy relay relays socket sockets port ports network networking wifi router modem
firewall proxy domain domains dns certificate certificates ssl backup backups restore storage disk disks drive
drives laptop laptops desktop computer computers phone phones tablet tablets monitor monitors keyboard keyboards
mouse printer printers scanner cable cables charger chargers firmware software hardware device devices os system
systems tool tools tooling workflow workflows automation terminal shell command commands cli sdk framework
frameworks stack microservice microservices algorithm algorithms prs tracker snapshot snapshots nodes vector vectors
embedding embeddings notebook notebooks macro macros formula formulas
# home and chores
kitchen bathroom bedroom bedrooms living dining room rooms closet closets garage basement attic yard lawn garden
porch patio fence gate driveway roof gutters gutter window windows door doors floor floors carpet rug rugs curtains
blinds shelf shelves shelving desk desks chair chairs couch sofa bed beds mattress towels pillows laundry clothes
shirt shirts pants jeans socks shoes boots jacket jackets coat coats dishes dishwasher sink faucet toilet shower tub
bath oven stove fridge freezer microwave pantry cabinet cabinets drawer drawers counter counters countertops trash
garbage recycling compost bin bins box boxes bag bags suitcase house home apartment flat furniture lamp lamps light
lights bulb bulbs battery batteries detector detectors alarm alarms filter filters vent vents heater furnace boiler
water pipe pipes drain drains paint walls wall ceiling trim baseboards grout tile tiles caulk handle handles lock
locks mailbox doorbell thermostat outlet outlets switch switches wiring plumbing hose sprinkler sprinklers mower
leaves snow steps stairs railing shed toolbox drill hammer nails screws bolts paintbrush ladder tape glue frame
frames picture pictures mirror mirrors decor plant plants pot pots planter planters soil seeds seed seedlings
tomatoes herbs flowers flower tree trees bush bushes hedge hedges weeds mulch vegetables veggies
# errands, shopping, admin
groceries grocery store stores shop shopping market mall bank office parcel parcels return returns exchange refund
refunds delivery deliveries pickup errands gas car cars oil tires tire brakes wash registration license renewal
insurance policy policies taxes tax savings retirement investment investments invest bills rent mortgage
subscription subscriptions credit card cards statement statements payroll paycheck salary raise bonus loan loans
transfer transfers deposit deposits passport warranty warranties utilities utility electricity electric internet
membership memberships gym
# food and cooking
dinner dinners lunch lunches breakfast breakfasts brunch meal meals snack snacks recipe recipes bread sourdough
pasta pizza soup soups salad salads sauce cookies cookie cake cakes pie pies muffins rice beans chili curry stew
tacos sandwich sandwiches chicken fish fruit coffee tea juice smoothie smoothies dough loaf loaves jam pickles
kimchi granola oatmeal pancakes waffles cookbook grill bbq picnic leftovers spices
# fitness and outdoors
run runs running jog jogging walk walks walking hike hikes hiking bike biking ride rides cycling swim swimming laps
lap mile km kilometers workout workouts weights squats pushups pullups situps plank planks stretches stretching
cardio marathon races trail trails climb climbing bouldering pilates tennis golf basketball soccer football
volleyball pickleball skiing ski snowboard snowboarding surf surfing kayak kayaking paddle rowing interval intervals
reps sets routine practice drills match matches game games league tournament park beach lake mountain mountains
camping tent campsite
# learning and hobbies
learn study courses lecture lectures exam exams quiz quizzes homework assignment assignments flashcards vocabulary
vocab grammar language languages conjugation conjugations words word phrases sentences piano guitar ukulele violin
cello drums bass song songs chords scales music album albums podcast podcasts video videos channel photo photos
photography camera drawing drawings painting paintings sketch sketches craft crafts knitting crochet sewing quilt
quilts woodworking pottery puzzle puzzles jigsaw crossword sudoku chess novel novels reading writing poem poems
poetry journal journals journaling comic comics zine collection collections scrapbook film films movie movies show
shows series episode episodes documentary documentaries lego hobby hobbies instrument instruments recital
# travel
trip trips flight flights hotel hotels booking bookings packing luggage airport train trains bus rental vacation
holiday holidays weekend getaway tour tours map maps route routes
# generic nouns
item items part parts piece pieces hour hours minute minutes time times day days week weeks month months year years
morning mornings afternoon evening evenings night nights weekends quarter quarters end start beginning middle half
rest batch round idea ideas option process approach method ways habit habits routines list priority priorities
progress status inventory supplies materials details question questions answer answers problem problems solution
solutions change changes improvement improvements cost costs number numbers amount amounts total totals count line
lines topic topics area areas space spaces corner side front back top bottom inside outside
# adjectives and adverbs
new old next last first second third final big small large little quick short long full whole entire main other
extra more less early late fresh clean dirty broken leaky slow quarterly monthly weekly daily annual yearly
remaining pending open overdue current upcoming simple basic advanced public private shared important urgent easy
outstanding existing missing unused unread spare nightly tonight together alone online offline remote digital
printed written spoken tall wide narrow deep shallow empty ready same different right correct wrong core upstairs
downstairs indoor outdoor
# more of the same: pets, tech, work, home, garden, travel, events, materials, learning, money
dog dogs cat cats pet pets puppy kitten birds horse horses flow flows loader loaders cloud clouds bucket buckets
handler handlers controller controllers middleware response responses payload payloads field fields column columns
row rows records path paths url urls image images asset assets icon icons font fonts color colors theme themes
button buttons modal modals input inputs validation suite suites coverage fixture fixtures stub stubs comment
comments typo typos warning warnings tag tags fork forks clone diff diffs chunk chunks thread threads speed speedup
profiles trace traces span spans postmortem uptime downtime security vulnerability vulnerabilities compliance
licenses stats analytics plot plots visualization visualizations bot bots chatbot setup teardown kickoff walkthrough
rundown breakdown buildout handbook playbook runbook culture headcount offboarding grant bookshelf bookshelves
bookcase wardrobe dresser nightstand blanket blankets linens bedding hamper ironing broom sponge dish pan pans knife
knives primer brush brushes roller rollers tarp cabin tomato peppers lettuce kale parsley cilantro mint trellis
tulips daffodils succulents cactus houseplant houseplants grass shrubs firewood wood travel roadtrip road roads
cruise ferry wedding weddings toast toasts speech speeches birthday birthdays anniversary barbecue metal glass
plastic cardboard fabric leather cotton wool steel brick concrete stone yellow orange purple pink gray grey gold
silver noon midnight dusk sunrise sunset statistics math maths calculus algebra geometry physics chemistry economics
accounting finance finances financial programming coding verb verbs noun nouns tense tenses kanji characters concept
concepts textbook textbooks driving latest newest recent previous initial rough spending income stocks chain chains
helmet gear gears bottle bottles wallet backpack umbrella gloves hat scarf headphones earbuds console date dates
parse parsing junk snake banana bananas subjunctive cartridge cartridges rack racks mil
""".splitlines() if not line.startswith("#") for word in line.split())
FUNCTION_WORDS = _words("""
a an the to of in on at by for with from about into onto over after before during until up down out off back and but
then so as than all some any every each this that these those my mine myself me i i'm i'll i've want wanna will
going gonna need have has is am be more less most least very also only both own around through across between under
above below near per via within toward towards how which where when while once twice one two three four five six
seven eight nine ten eleven twelve fifteen twenty thirty forty fifty hundred thousand first second third fourth
fifth half dozen couple few several many much another other same
""")
# What a capitalised word may be without naming a person or an organisation the rule cannot vet.
SAFE_ACRONYMS = _words("""
PR PRs API APIs UI UX CI CD QA SQL CSS HTML JSON CSV PDF PDFs SDK CLI DB OKR OKRs KPI KPIs MVP ETA FAQ README SEO CRM
RFC SLA SOP PRD ADR AWS GCP VPN DNS SSH SSL TLS HTTP HTTPS REST GPU CPU RAM SSD USB TV AC HVAC DIY ASAP AM PM EOD EOW
IRA LLC CV MBA ML AI LLM NLP OS VM IDE JS TS PHP ORM ETL BI CMS
""")
SAFE_PROPER = _words("""
android angular anki ansible arabic asana azure bitbucket cantonese chinese chrome confluence danish debian deno
django docker duolingo dutch emacs english excel fastapi figma finnish firefox flask french german github gitlab
gmail golang google greek hebrew hindi ios ipados italian japanese java javascript jira kafka keynote kindle korean
kotlin kubernetes linux macos mandarin mongodb mysql node nodejs norwegian notion polish portuguese postgres
postgresql powerpoint python rails react redis russian rust safari slack spanish spotify sqlite svelte swedish swift
terraform thai trello turkish typescript ubuntu vietnamese vim vscode vue windows xcode youtube zoom
""")
I_FORMS = _words("I I'm I'll I've I'd")
_KNOWN = (VETTED | FUNCTION_WORDS | TASK_VERBS | NONPERSON | SPAN_SKIP | SPAN_END | PARTICLES | TIME_WORDS | SAFE_PROPER
          | eg.STOPWORDS | eg.NUMBER_WORDS | frozenset(word.lower() for word in SAFE_ACRONYMS | I_FORMS))
# The ordinary words a person's name may contain ("Park"): never read as the person.
_COMMON = _KNOWN | _words("i me my we our you your he she they it its his her their is am are was were be been do does "
                          "did will would can could should may might must have has had not no yes and or but if")
_SPECIAL_WORDS = eg.SPECIAL | SPECIAL_EXTRA
_PEOPLE = eg.THIRD_PARTY | THIRD_PARTY_EXTRA
_PEOPLE_STEMS = frozenset(eg.stem(word) for word in _PEOPLE if len(word) > 3)


# --- the goal text ---------------------------------------------------------------------------------

def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("\u2019", "'").replace("\u02bc", "'")


def _plain(word: str) -> str:
    """Casefolded, without combining marks ("fianc\u00e9e" reads as "fiancee")."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", word.casefold()) if not unicodedata.combining(ch))


def _has(words: list, phrase) -> bool:
    n = len(phrase)
    return any(tuple(words[i:i + n]) == tuple(phrase) for i in range(len(words) - n + 1))


def _starts(words: list, at: int, options) -> int:
    """The length of the longest option the words start with at ``at``, or 0."""
    return max((len(option) for option in options if tuple(words[at:at + len(option)]) == option), default=0)


def _characters(text: str) -> str | None:
    """The first character-level reason this is not one plain typed clause, or None."""
    if unicodedata.normalize("NFKC", text) != text:
        return "goal_field_shape"            # compatibility forms, decomposed marks: never what the app renders
    last = len(text) - 1
    for index, ch in enumerate(text):
        category = unicodedata.category(ch)
        if category[0] == "C":
            return "goal_field_shape"        # controls, format characters (zero-width), unassigned
        if category[0] == "L":
            if not unicodedata.name(ch, "").startswith("LATIN "):
                return "goal_field_shape"    # the guards read English: a word they cannot read withholds
            continue
        if ch in "0123456789 ":
            continue
        before = text[index - 1] if index else ""
        after = text[index + 1] if index < last else ""
        if ch in "'\u2019":
            if before.isalnum() and (after.isalnum() or before in "sS"):
                continue                     # "don't", "today's", and a plural possessive
            return "goal_field_question_or_quote"
        if ch == "-" and before.isalnum() and after.isalnum():
            continue
        if ch in ".!" and index == last and before.isalnum():
            continue
        if ch in ",:" and before.isdigit() and after.isdigit():
            continue
        if ch == "%" and before.isdigit():
            continue
        if ch == "?" or ch in _QUOTES:
            return "goal_field_question_or_quote"
        return "goal_field_not_intention"    # a URL, a list title, a placeholder, a quote's dash: not one clause
    return None


def text_refusal(goal, *, boundary, people=frozenset()) -> str | None:
    """The guards on the goal text itself: the first that withholds, as a code, or None. ``people``: name words
    the node knows belong to someone else."""
    from .permitted_derivation import Spec, refusal as lane_refusal
    if type(goal) is not str or not goal.strip():
        return "goal_field_shape"
    deferred = _characters(goal)
    if deferred in ("goal_field_shape", "goal_field_question_or_quote"):
        return deferred
    if lane_refusal(Spec("goal", "goal", goal), None) is not None:
        return "goal_field_shape"            # the lane's own goal shape: 6-300 characters, two words, one line
    raw = _TOKEN.findall(_fold(goal))
    low = [word.casefold() for word in raw]
    plain = [_plain(word) for word in raw]
    if not raw or len(raw) > MAX_WORDS:
        return "goal_field_shape"
    if boundary is None:
        return "goal_field_boundary_unavailable"
    try:
        if boundary.mentions_protected(goal):
            return "goal_field_offlimits"
    except Exception:  # noqa: BLE001 -- an Off-limits check that cannot answer withholds
        return "goal_field_boundary_unavailable"
    verb_at = _intention_verb(low)
    if _special(plain, verb_at):
        return "goal_field_special_category"
    if deferred is not None:
        return deferred
    words = set(plain)
    opening = _starts(low, 0, LEADING_TIME)
    if (opening < len(low) and low[opening] in QUESTION_START) or (
            verb_at is not None and low[verb_at] == "do" and verb_at + 1 < len(low) and low[verb_at + 1] in SUBJECTS):
        return "goal_field_question_or_quote"
    if eg._reports(plain):
        return "goal_field_reported"
    if eg.NEGATIONS & words or any(w.endswith("n't") or (w.endswith("nt") and w[:-2] + "n't" in eg._NT) for w in words):
        return "goal_field_negated"
    if (eg.HEDGES | HEDGES_EXTRA) & words or any(_has(plain, phrase) for phrase in eg.HEDGE_PHRASES):
        return "goal_field_hedged"
    if ((eg.SARCASM | SARCASM_EXTRA) & words or any(_has(plain, phrase) for phrase in eg.SARCASM_PHRASES)
            or any(sign in goal for sign in eg.SARCASM_SIGNS) or re.search(r"([^\W\d_])\1{3,}", goal.casefold())
            or _shouted(raw)):
        return "goal_field_sarcasm"
    if ENDED_GOAL & words or any(_has(eg._without_used_to_idiom(plain), phrase) for phrase in eg.ENDED_PHRASES):
        return "goal_field_ended"
    if NOT_YET & words or any(_has(plain, phrase) for phrase in NOT_YET_PHRASES):
        return "goal_field_not_yet"
    if _third_party(raw, low, plain, verb_at, goal) or people & set(plain):
        return "goal_field_third_party"
    if not all(_vetted(word) for word in plain):
        return "goal_field_unvetted_word"
    if verb_at is None or _not_intention(low, plain, verb_at):
        return "goal_field_not_intention"
    return None


def _vetted(word: str) -> bool:
    """A word the rule has vetted: on a list, by its OD-38 stem, a number or code ("5k", "q3"), or a contraction of one
    ("today's", "i'm")."""
    if word in _KNOWN or eg.stem(word) in _KNOWN or any(ch.isdigit() for ch in word):
        return True
    base, apostrophe, suffix = word.partition("'")
    return (bool(apostrophe) and suffix in ("s", "m", "ll", "ve", "d", "re")
            and (base in _KNOWN or eg.stem(base) in _KNOWN))


def _intention_verb(low: list) -> int | None:
    """Where the task verb stands: after an optional time word, an optional first-person prefix and an optional
    pre-verb word. None when the field does not open that way."""
    at = _starts(low, 0, LEADING_TIME)
    at += _starts(low, at, FIRST_PERSON)
    if at + 1 < len(low) and low[at] in PRE_VERB and low[at + 1] in TASK_VERBS:
        at += 1
    return at if at < len(low) and low[at] in TASK_VERBS else None


def _special(plain: list, verb_at) -> bool:
    for index, word in enumerate(plain):
        if word in SPECIAL_UNLESS_VERB and (index == verb_at or (
                word == "smoke" and index + 1 < len(plain) and plain[index + 1] in ("test", "tests", "testing"))):
            continue
        root = eg.stem(word)
        if (word in _SPECIAL_WORDS or root in _SPECIAL_WORDS or any(part in word for part in SPECIAL_ROOTS)
                or (word not in SUFFIX_EXEMPT and len(word) > 5 and word.endswith(SPECIAL_SUFFIXES))):
            return True
    return any(_has(plain, phrase) for phrase in SPECIAL_PHRASES)


def _shouted(raw: list) -> bool:
    """A word in capitals that is not a known acronym: shouting, or an organisation the rule cannot vet."""
    for word in raw:
        letters = [ch for ch in word if ch.isalpha()]
        if (len(letters) >= 2 and all(ch.isupper() for ch in letters) and not any(ch.isdigit() for ch in word)
                and word not in SAFE_ACRONYMS and word not in I_FORMS):
            return True
    return False


def _third_party(raw: list, low: list, plain: list, verb_at, goal: str) -> bool:
    for index, (original, word) in enumerate(zip(raw, plain)):
        if word in _PEOPLE or (len(word) > 3 and eg.stem(word) in _PEOPLE_STEMS):
            return True
        if word in PERSON_VERBS or eg.stem(word) in PERSON_VERBS:
            return True
        if word.endswith(PERSON_SUFFIXES) and word not in PERSON_SUFFIX_EXEMPT and len(word) > 4:
            return True                       # a person by trade or field
        if word.endswith("'s") and word[:-2] not in TIME_WORDS:
            return True                       # someone's: a possessive other than a time's
        if index and _name_like(original):
            return True                       # a capitalised word that is not a month, a day, an acronym or a tool
    if any(_has(plain, phrase) for phrase in THIRD_PARTY_PHRASES):
        return True
    for match in re.finditer(r"([^\W\d_]+s)'(?=\s|$|[.!])", _fold(goal)):
        if _plain(match.group(1)) not in TIME_WORDS:
            return True                       # a plural possessive; "two weeks'" is a time's
    if verb_at is not None and low[verb_at] in CONTACT_VERBS:
        span, at = [], verb_at + 1
        while at < len(low) and low[at] in PARTICLES:
            at += 1
        while at < len(low) and low[at] not in SPAN_END:
            span.append(plain[at])
            at += 1
        if any(word not in NONPERSON and word not in SPAN_SKIP and not any(ch.isdigit() for ch in word)
               for word in span):
            return True                       # an object that may be a person
    return False


def _name_like(original: str) -> bool:
    if original in I_FORMS or not any(ch.isupper() for ch in original) or any(ch.isdigit() for ch in original):
        return False                          # lowercase, "I", or a code such as "Q3"
    folded = _plain(original)
    if folded in TIME_WORDS or folded in SAFE_PROPER or original in SAFE_ACRONYMS:
        return False
    return not (original.endswith("s") and original[:-1] in SAFE_ACRONYMS)


def _not_intention(low: list, plain: list, verb_at: int) -> bool:
    words, after = set(plain), set(plain[verb_at + 1:])
    if PLACEHOLDERS & words or any(_has(plain, phrase) for phrase in PLACEHOLDER_PHRASES):
        return True
    if len(plain) == 2 and plain[1] in LIST_NOUNS:
        return True                           # a list's title
    if verb_at + 1 < len(low) and low[verb_at + 1] in MOTTO_NEXT:
        return True                           # a motto
    return bool(VAGUE & after)                # a motto's or a quote's vocabulary, or no object to speak of
