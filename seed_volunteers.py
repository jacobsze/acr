"""
One-time script to seed volunteer records.
Run via Render Shell: python seed_volunteers.py
"""
from app import create_app
from models import db, User

VOLUNTEERS = [
    ("Emma Lindsay",          "emmalindsay@yopmail.com",          "123-123-1234"),
    ("Emily Larkin",          "emilylarkin@yopmail.com",           "123-123-1234"),
    ("Molly Macleod",         "mollymacleod@yopmail.com",          "123-123-1234"),
    ("Olivia Luchini",        "olivialuchini@yopmail.com",         "123-123-1234"),
    ("DeeDee Han",            "deedeehen@yopmail.com",             "123-123-1234"),
    ("Connor Noonan",         "connornoonan@yopmail.com",          "123-123-1234"),
    ("Paige Truong",          "paigetruong@yopmail.com",           "123-123-1234"),
    ("Maryna Pyrogova",       "marynapyrogova@yopmail.com",        "123-123-1234"),
    ("Amanda Santiago",       "amandasantiago@yopmail.com",        "123-123-1234"),
    ("Lena Mills",            "lenamills@yopmail.com",             "123-123-1234"),
    ("Keith Sabalja",         "keithsabalja@yopmail.com",          "123-123-1234"),
    ("Lindsey Miller",        "lindseymiller@yopmail.com",         "123-123-1234"),
    ("Lisa Saracuse",         "lisasaracuse@yopmail.com",          "123-123-1234"),
    ("Athmeeya Mohite",       "athmeeyamohite@yopmail.com",        "123-123-1234"),
    ("Romia Khan",            "romiakhan@yopmail.com",             "123-123-1234"),
    ("Samantha Schoenberger", "samanthaschoenberger@yopmail.com",  "123-123-1234"),
    ("Christina Shea-Wright", "christinashea-wright@yopmail.com",  "123-123-1234"),
    ("Sophia Yau",            "sophiayau@yopmail.com",             "123-123-1234"),
    ("Patrick Biedermann",    "patrickbiedermann@yopmail.com",     "123-123-1234"),
    ("Susan Weiswasser",      "susanweiswasser@yopmail.com",       "123-123-1234"),
    ("Dana Sagona",           "danasagona@yopmail.com",            "123-123-1234"),
    ("Karen McCabe",          "karenmccabe@yopmail.com",           "123-123-1234"),
    ("Raquel Conard",         "raquelconard@yopmail.com",          "123-123-1234"),
    ("Travis Shaffer",        "travisshaffer@yopmail.com",         "123-123-1234"),
    ("Gabi Ramsey",           "gabiramsey@yopmail.com",            "123-123-1234"),
    ("Karen Salama",          "karensalama@yopmail.com",           "123-123-1234"),
    ("Jared Kessel",          "jaredkessel@yopmail.com",           "123-123-1234"),
    ("Rose",                  "rose@yopmail.com",                  "123-123-1234"),
    ("Meg Gordon",            "meggordon@yopmail.com",             "123-123-1234"),
    ("Ikra Ali",              "ikraali@yopmail.com",               "123-123-1234"),
    ("Daniela Martinez",      "danielamartinez@yopmail.com",       "123-123-1234"),
    ("Jasmin Bolduc",         "jasminbolduc@yopmail.com",          "123-123-1234"),
    ("Steve Polvino",         "stevepolvino@yopmail.com",          "123-123-1234"),
    ("Sydney Wolk",           "sydneywolk@yopmail.com",            "123-123-1234"),
    ("Claudia Zavalloni",     "claudiazavalloni@yopmail.com",      "123-123-1234"),
]

app = create_app()
with app.app_context():
    added = 0
    skipped = 0
    for name, email, phone in VOLUNTEERS:
        if User.query.filter_by(email=email).first():
            print(f"  skip  {name} (already exists)")
            skipped += 1
        else:
            db.session.add(User(name=name, email=email, phone=phone, role="volunteer"))
            print(f"  add   {name}")
            added += 1
    db.session.commit()
    print(f"\nDone: {added} added, {skipped} skipped.")
