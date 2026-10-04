"""Données de démo : un cabinet fictif avec des tâches dans tous les états du cycle de vie.

Toutes les personnes, clients et dossiers sont inventés.
Lancement manuel : `python -m app.seed` (réinitialise la base).
"""
import json
import random
import unicodedata
from datetime import date, datetime, timedelta

from sqlalchemy import MetaData
from sqlalchemy.orm import Session

from app import clock
from app.db import Base, SessionLocal, engine
from app.models import (CalendarEvent, Client, Conflict, Delegation, DelegationStatus, Matter, MatterStatus, Proposal,
                        ProposalStatus, Role, Specialty, SubSpecialty, Task, TaskEvent, TaskStatus, TimeEntry,
                        TimeEntryStatus, User, UserSpecialty, UserSubSpecialty)
from app.services import ai

# Domaines du droit et leurs sous-spécialités
SPECIALTIES = {
    "Droit des sociétés": ["Fusions-acquisitions", "Secrétariat juridique et gouvernance", "Pactes d'associés"],
    "Droit social": ["Contentieux prud'homal", "Relations collectives", "Rupture du contrat de travail"],
    "Contentieux commercial": ["Rupture brutale et distribution", "Recouvrement de créances", "Référés et urgence"],
    "Droit immobilier": ["Baux commerciaux", "Construction", "Transactions immobilières"],
    "Propriété intellectuelle": ["Marques", "Brevets", "Contrefaçon et logiciels"],
    "Droit pénal des affaires": ["Abus de biens sociaux et fraude", "Corruption et compliance", "Enquêtes internes"],
}

# Profils. domains : {domaine: (niveau, dossiers traités)} ; subs : {sous-spécialité: (niveau, dossiers traités)}
# types : {type de dossier: nombre traité} ; prefs : recherche, rédaction, négociation, contentieux
PEOPLE = [
    dict(key="Exemple associe", name="Exemple", role=Role.PARTNER, bar=2008, joined=2012, rate=500,
         langs="Français, Anglais", domains={"Droit des sociétés": (3, 60), "Contentieux commercial": (3, 45)},
         bio="Compte de démonstration (associé) : tapez « exemple » à la connexion."),
    dict(key="Hélène Marchal", role=Role.FOUNDING_PARTNER, bar=2003, joined=2009, rate=550, langs="Français, Anglais",
         domains={"Droit des sociétés": (3, 120), "Contentieux commercial": (3, 80)},
         bio="Partner fondatrice, opérations de haut de bilan et contentieux des affaires."),
    dict(key="Antoine Ferrand", role=Role.PARTNER, bar=2006, joined=2011, rate=520, langs="Français, Allemand",
         domains={"Droit social": (3, 90), "Droit pénal des affaires": (3, 40)},
         bio="Associé, relations sociales collectives et pénal des affaires."),

    dict(key="Exemple collaborateur", name="Exemple", role=Role.ASSOCIATE, bar=2017, joined=2019, rate=300,
         langs="Français, Anglais", domains={"Contentieux commercial": (2, 18), "Droit immobilier": (2, 14)},
         subs={"Rupture brutale et distribution": (2, 7), "Recouvrement de créances": (2, 9), "Baux commerciaux": (2, 10)},
         types={"conclusions": 12, "audit": 5, "consultation": 6}, sectors="Industrie, Immobilier",
         countries="France, Royaume-Uni", legal="Droit français, Droit de l'UE", juris="Paris, Versailles",
         prefs="contentieux, rédaction", lit=2, trans=1, intl=1,
         bio="Compte de démonstration (collaborateur) : contentieux commercial, baux commerciaux, recouvrement."),
    dict(key="Camille Bernard", role=Role.ASSOCIATE, bar=2016, joined=2016, rate=320, langs="Français, Anglais",
         domains={"Droit des sociétés": (3, 40), "Contentieux commercial": (2, 10)},
         subs={"Fusions-acquisitions": (3, 15), "Secrétariat juridique et gouvernance": (3, 25),
               "Pactes d'associés": (2, 8), "Rupture brutale et distribution": (1, 3)},
         types={"acte": 30, "audit": 12, "negociation": 8, "formalites": 20, "conclusions": 3},
         sectors="Industrie, Distribution", countries="France, Royaume-Uni, États-Unis",
         legal="Droit français, Common law", juris="Paris", prefs="négociation, rédaction", lit=1, trans=3, intl=2,
         bio="Cessions de titres, pactes d'associés, secrétariat juridique de groupes, due diligence."),
    dict(key="Julien Moreau", role=Role.ASSOCIATE, bar=2021, joined=2021, rate=220, langs="Français",
         domains={"Droit social": (3, 35)},
         subs={"Contentieux prud'homal": (3, 25), "Rupture du contrat de travail": (3, 20), "Relations collectives": (1, 2)},
         types={"conclusions": 20, "acte": 15, "consultation": 10}, sectors="Agroalimentaire, Distribution",
         juris="Paris, Créteil", prefs="contentieux, rédaction", lit=3, trans=1,
         bio="Contentieux prud'homal, licenciements, ruptures conventionnelles, relations avec le CSE."),
    dict(key="Sarah Benali", role=Role.ASSOCIATE, bar=2012, joined=2014, rate=380, langs="Français, Arabe, Anglais",
         domains={"Contentieux commercial": (3, 70), "Droit immobilier": (2, 15)},
         subs={"Rupture brutale et distribution": (3, 22), "Recouvrement de créances": (3, 30),
               "Référés et urgence": (3, 18), "Baux commerciaux": (2, 9)},
         types={"conclusions": 60, "consultation": 15, "negociation": 6}, sectors="Industrie, Transport et logistique",
         countries="France, Maroc", legal="Droit français, Droit de l'UE", juris="Paris, Versailles, Nanterre",
         prefs="contentieux, négociation", lit=3, trans=1, intl=1,
         bio="Contentieux commercial devant le tribunal des activités économiques et la cour d'appel, rupture brutale, "
             "recouvrement de créances, baux commerciaux."),
    dict(key="Thomas Leroy", role=Role.ASSOCIATE, bar=2023, joined=2023, rate=180, langs="Français, Espagnol",
         domains={"Droit des sociétés": (2, 8), "Propriété intellectuelle": (1, 3)},
         subs={"Secrétariat juridique et gouvernance": (2, 6), "Marques": (1, 3)},
         types={"formalites": 10, "acte": 4}, sectors="Design et luxe", countries="France, Espagne",
         juris="Paris", prefs="recherche, rédaction", trans=1,
         bio="Constitution de sociétés, assemblées générales, approbation des comptes, modifications statutaires."),
    dict(key="Inès Garnier", role=Role.ASSOCIATE, bar=2018, joined=2020, rate=290, langs="Français, Anglais, Italien",
         domains={"Propriété intellectuelle": (3, 45), "Droit des sociétés": (1, 4)},
         subs={"Marques": (3, 30), "Contrefaçon et logiciels": (3, 12), "Brevets": (1, 2)},
         types={"formalites": 25, "consultation": 10, "conclusions": 4, "negociation": 5},
         sectors="Design et luxe", countries="France, Italie", legal="Droit français, Droit de l'UE",
         juris="Paris", prefs="recherche, négociation", lit=1, trans=2, intl=2,
         bio="Marques, dépôts INPI et EUIPO, recherches d'antériorité, contrefaçon, licences de logiciel."),
    dict(key="Maxime Fontaine", role=Role.ASSOCIATE, bar=2015, joined=2017, rate=340, langs="Français",
         domains={"Droit pénal des affaires": (3, 35), "Contentieux commercial": (2, 12)},
         subs={"Abus de biens sociaux et fraude": (3, 15), "Corruption et compliance": (2, 6),
               "Enquêtes internes": (3, 10), "Rupture brutale et distribution": (1, 2)},
         types={"conclusions": 25, "consultation": 15, "audit": 6}, sectors="Transport et logistique, Industrie",
         juris="Paris, Nanterre", prefs="contentieux, recherche", lit=3,
         bio="Abus de biens sociaux, corruption, fraude, garde à vue, instruction, plainte avec constitution de partie civile."),
    dict(key="Léa Dubois", role=Role.ASSOCIATE, bar=2020, joined=2020, rate=240, langs="Français, Anglais",
         domains={"Droit immobilier": (3, 30), "Droit social": (1, 3)},
         subs={"Baux commerciaux": (3, 20), "Transactions immobilières": (2, 8), "Construction": (2, 5),
               "Rupture du contrat de travail": (1, 2)},
         types={"audit": 15, "acte": 12, "consultation": 6}, sectors="Immobilier", juris="Paris",
         prefs="rédaction, recherche", lit=1, trans=2,
         bio="Baux commerciaux, copropriété, ventes immobilières, construction et promotion."),
    dict(key="Hugo Lambert", role=Role.ASSOCIATE, bar=2019, joined=2022, rate=260, langs="Français, Anglais",
         domains={"Droit social": (2, 14), "Contentieux commercial": (2, 10)},
         subs={"Contentieux prud'homal": (2, 10), "Rupture du contrat de travail": (2, 6),
               "Rupture brutale et distribution": (2, 5), "Recouvrement de créances": (2, 6)},
         types={"conclusions": 18, "audit": 4}, sectors="Agroalimentaire", countries="France, Irlande",
         juris="Paris, Versailles", prefs="contentieux", lit=2, trans=1, intl=1,
         bio="Contentieux prud'homal et commercial, audits sociaux, mise en demeure."),

    dict(key="Exemple stagiaire", name="Exemple", role=Role.INTERN, joined=2026, rate=90, langs="Français, Anglais",
         domains={"Droit social": (1, 2), "Droit des sociétés": (1, 1)},
         subs={"Contentieux prud'homal": (1, 2), "Secrétariat juridique et gouvernance": (1, 1)},
         types={"consultation": 2, "formalites": 1}, prefs="recherche, rédaction",
         bio="Compte de démonstration (stagiaire) : tapez « exemple » à la connexion."),
    dict(key="Chloé Martin", role=Role.INTERN, joined=2026, rate=90, langs="Français, Anglais",
         domains={"Droit des sociétés": (1, 2)}, subs={"Secrétariat juridique et gouvernance": (1, 3)},
         types={"formalites": 4}, prefs="rédaction",
         bio="Élève-avocate, stage en droit des sociétés : procès-verbaux d'assemblée, statuts, registres."),
    dict(key="Nathan Roux", role=Role.INTERN, joined=2026, rate=90, langs="Français",
         domains={"Droit social": (1, 1)}, subs={"Contentieux prud'homal": (1, 2), "Rupture du contrat de travail": (1, 1)},
         types={"consultation": 2}, prefs="recherche",
         bio="Élève-avocat, recherches en droit du travail, préparation de dossiers prud'homaux."),
    dict(key="Emma Petit", role=Role.INTERN, joined=2026, rate=90, langs="Français, Anglais",
         domains={"Propriété intellectuelle": (1, 2), "Contentieux commercial": (1, 1)},
         subs={"Marques": (1, 3), "Rupture brutale et distribution": (1, 1), "Recouvrement de créances": (1, 1)},
         types={"consultation": 2, "conclusions": 1}, prefs="recherche, rédaction",
         bio="Stagiaire, recherches d'antériorité de marques et veille jurisprudentielle."),
    dict(key="Yanis Chevalier", role=Role.INTERN, joined=2026, rate=90, langs="Français, Arabe",
         domains={"Droit immobilier": (1, 1), "Droit pénal des affaires": (1, 1), "Contentieux commercial": (1, 1)},
         subs={"Baux commerciaux": (1, 2), "Abus de biens sociaux et fraude": (1, 1),
               "Rupture brutale et distribution": (1, 1)},
         types={"audit": 1, "conclusions": 1}, prefs="recherche, contentieux",
         bio="Élève-avocat, recherches en droit immobilier, pénal des affaires et contentieux."),
]

# Clés des comptes de démonstration dans le dictionnaire `users` (les deux s'appellent « Exemple »)
EXAMPLE_PARTNER = "Exemple associe"
EXAMPLE_ASSOCIATE = "Exemple collaborateur"
EXAMPLE_INTERN = "Exemple stagiaire"

CLIENTS = {  # nom: (secteur, langue maternelle, pays)
    "Nordis Industrie": ("Industrie", "Anglais", "Royaume-Uni"),
    "Boulangeries Lemaire": ("Agroalimentaire", "Français", "France"),
    "Atelier Vasco": ("Design et luxe", "Italien", "Italie"),
    "Groupe Hestia Immobilier": ("Immobilier", "Français", "France"),
    "Transports Celtis": ("Transport et logistique", "Français", "France"),
}
CONFLICTS = [  # (personne, client, motif)
    ("Sarah Benali", "Transports Celtis", "A représenté la partie adverse en 2024"),
    ("Thomas Leroy", "Groupe Hestia Immobilier", "Lien familial avec un dirigeant"),
]


def reset_database() -> None:
    """Supprime TOUTES les tables présentes (y compris celles d'un ancien schéma), puis recrée la démo."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        sqlite = conn.dialect.name == "sqlite"
        if sqlite:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        existing = MetaData()
        existing.reflect(conn)
        existing.drop_all(conn)
        if sqlite:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    ai.reset_state()
    with SessionLocal() as db:
        seed(db)
        db.commit()


def _at(day: date, hour: float) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour)


def _next_weekday(d: date, n: int) -> date:
    """n-ième jour ouvré à partir de d (n=0 : d ou le suivant si week-end)."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    for _ in range(n):
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


def seed(db: Session) -> None:
    clock.reset(db)
    now = clock.now(db)
    today = now.date()
    rng = random.Random(42)

    specialties = {name: Specialty(name=name) for name in SPECIALTIES}
    subs = {sub: SubSpecialty(specialty=specialties[dom], name=sub) for dom, names in SPECIALTIES.items() for sub in names}
    db.add_all([*specialties.values(), *subs.values()])

    users: dict[str, User] = {}
    for p in PEOPLE:
        key = p["key"]
        slug = unicodedata.normalize("NFKD", key.lower()).encode("ascii", "ignore").decode().replace(" ", ".")
        user = User(name=p.get("name", key), email=f"{slug}@cabinet-demo.fr", role=p["role"], bar_year=p.get("bar"),
                    joined_year=p.get("joined"), hourly_rate=p["rate"], languages=p["langs"], bio=p["bio"],
                    weekly_capacity_hours=45 if p["role"] != Role.INTERN else 35,
                    sectors=p.get("sectors", ""), countries=p.get("countries", "France"),
                    legal_systems=p.get("legal", "Droit français"), jurisdictions=p.get("juris", "Paris"),
                    preferences=p.get("prefs", ""), litigation_level=p.get("lit", 0),
                    transactional_level=p.get("trans", 0), international_level=p.get("intl", 0),
                    task_type_counts=json.dumps(p.get("types", {})))
        user.specialties = [UserSpecialty(specialty=specialties[d], level=lvl, cases_count=n)
                            for d, (lvl, n) in p["domains"].items()]
        user.sub_specialties = [UserSubSpecialty(sub_specialty=subs[sub], level=lvl, cases_count=n)
                                for sub, (lvl, n) in p.get("subs", {}).items()]
        users[key] = user
    db.add_all(users.values())

    clients = {name: Client(name=name, sector=sector, language=lang, country=country)
               for name, (sector, lang, country) in CLIENTS.items()}
    db.add_all(clients.values())
    db.flush()
    for person, client, reason in CONFLICTS:
        db.add(Conflict(user_id=users[person].id, client_id=clients[client].id, reason=reason))

    # ------------------------------------------------------------------ agendas (3 semaines)
    templates = [("audience", "Audience – tribunal de commerce", 9, 3), ("audience", "Audience – conseil de prud'hommes", 14, 3),
                 ("rdv", "Rendez-vous client", 10, 1.5), ("rdv", "Réunion d'équipe", 9, 1),
                 ("rdv", "Conférence téléphonique client", 16, 1), ("formation", "Formation continue", 14, 4)]
    for user in users.values():
        if user.is_assigner:
            continue
        busy_factor = {"Camille Bernard": 3, "Sarah Benali": 2}.get(user.name, 1)
        for offset in range(0, 22):
            day = today + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            taken: list[tuple[float, float]] = []
            for _ in range(rng.randint(0, 1 + busy_factor)):
                kind, title, hour, length = rng.choice(templates)
                if user.role == Role.INTERN and kind == "audience":
                    title = "Assister à une audience"
                if any(not (hour + length <= a or hour >= b) for a, b in taken):
                    continue
                taken.append((hour, hour + length))
                db.add(CalendarEvent(user_id=user.id, title=title, kind=kind,
                                     start=_at(day, hour), end=_at(day, hour + length)))
    # Hugo Lambert en congé à partir du prochain lundi (démontre l'exclusion « disponibilité minimale »)
    leave_start = today + timedelta(days=(7 - today.weekday()))
    db.add(CalendarEvent(user_id=users["Hugo Lambert"].id, title="Congés", kind="conge",
                         start=_at(leave_start, 0), end=_at(leave_start + timedelta(days=5), 0)))
    db.flush()

    # ------------------------------------------------------------------ dossiers et tâches
    def event(kind: str, message: str, actor: User | None, when: datetime, m: Matter, t: Task | None = None):
        db.add(TaskEvent(matter_id=m.id, task_id=t.id if t else None, actor_id=actor.id if actor else None,
                         kind=kind, message=message, created_at=when))

    def matter(ref, name, client, specialty, sub, mtype, complexity, hours, deadline, creator, status,
               description="", **extra) -> Matter:
        m = Matter(reference=ref, name=name, description=description, client=clients[client],
                   specialty=specialties[specialty], sub_specialty=subs[sub] if sub else None, task_type=mtype,
                   complexity=complexity, estimated_hours=hours, deadline=deadline, created_by=users[creator],
                   status=status, created_at=extra.pop("created_at", now - timedelta(days=3)), **extra)
        db.add(m)
        db.flush()
        event("created", f"Dossier ouvert par {m.created_by.name}", m.created_by, m.created_at, m)
        return m

    def assign(m: Matter, person: str, when: datetime) -> None:
        m.assignee = users[person]
        m.budget_amount = m.estimated_hours * m.assignee.hourly_rate
        db.flush()
        event("accepted", f"Dossier accepté par {m.assignee.name}", m.assignee, when, m)

    def task(m: Matter, title, description, hours, deadline, status, type_=None, sub=None, delegate=None,
             created_at=None, completed_at=None) -> Task:
        owner = m.assignee
        t = Task(matter=m, title=title, description=description, specialty=m.specialty,
                 sub_specialty=subs[sub] if sub else m.sub_specialty, task_type=type_, complexity=m.complexity,
                 estimated_hours=hours, deadline=deadline, created_by=owner, assignee=owner,
                 delegate=users[delegate] if delegate else None, status=status,
                 created_at=created_at or now - timedelta(days=2), completed_at=completed_at,
                 budget_amount=hours * owner.hourly_rate)
        db.add(t)
        db.flush()
        event("created", f"Tâche « {title} » créée par {owner.name}", owner, t.created_at, m, t)
        return t

    def entries(t: Task, person: str, items: list[tuple[int, float, str, str]], status: str) -> None:
        user = users[person]
        for days_ago, hours, note, label in items:
            db.add(TimeEntry(user_id=user.id, task_id=t.id, work_date=today - timedelta(days=days_ago),
                             minutes=int(hours * 60), note=note, label=label, rate=user.hourly_rate,
                             billable=user.role != Role.INTERN, status=status,
                             created_at=now - timedelta(days=days_ago)))

    def deadline(days: int, hour: int = 18) -> datetime:
        return _at(today + timedelta(days=days), hour)

    # Historique ancien (clôturé) : donne de la « connaissance client » au matching
    old = matter("2026-009", "Nordis – mise en demeure fournisseur", "Nordis Industrie", "Contentieux commercial",
                 "Rupture brutale et distribution", "consultation", 1, 3, now - timedelta(days=60), "Hélène Marchal",
                 MatterStatus.CLOSED, "Mise en demeure pour livraisons non conformes.",
                 created_at=now - timedelta(days=70), closed_at=now - timedelta(days=60))
    assign(old, "Sarah Benali", now - timedelta(days=69))
    t_old = task(old, "Rédiger la mise en demeure", "Mise en demeure adressée au fournisseur.", 3,
                 now - timedelta(days=62), TaskStatus.DONE, "acte", created_at=now - timedelta(days=68),
                 completed_at=now - timedelta(days=62))
    entries(t_old, "Sarah Benali", [(63, 2.5, "rédaction mise en demeure",
                                     "Rédaction d'une mise en demeure adressée au fournisseur pour livraisons non "
                                     "conformes.")], TimeEntryStatus.INVOICED)

    # 1. À attribuer, échéance à J-5 : l'associé est notifié dès le premier passage du planificateur
    matter("2026-014", "Nordis – litige fournisseur (rupture brutale)", "Nordis Industrie", "Contentieux commercial",
           "Rupture brutale et distribution", "conclusions", 3, 14, deadline(5), EXAMPLE_PARTNER, MatterStatus.TO_ASSIGN,
           "Le fournisseur assigne Nordis devant le tribunal des activités économiques pour rupture brutale des "
           "relations commerciales. Conclusions en réponse à déposer (environ 40 pages), avec demande "
           "reconventionnelle sur les livraisons non conformes et la créance de pénalités.",
           ai_summary="Dossier complexe en contentieux commercial : conclusions en réponse et demande reconventionnelle.")

    # 2. À attribuer, J-6 (associé Antoine Ferrand)
    matter("2026-032", "Lemaire – rupture conventionnelle d'un cadre", "Boulangeries Lemaire", "Droit social",
           "Rupture du contrat de travail", "acte", 2, 5, deadline(6), "Antoine Ferrand", MatterStatus.TO_ASSIGN,
           "Rupture conventionnelle d'un responsable de production : calcul de l'indemnité, calendrier des "
           "entretiens, formulaire d'homologation.")

    # 3. À attribuer, échéance lointaine : pas encore notifié (avancer le temps pour le voir arriver)
    matter("2026-040", "Vasco – dépôt de la marque « Linea »", "Atelier Vasco", "Propriété intellectuelle", "Marques",
           "formalites", 1, 4, deadline(16), EXAMPLE_PARTNER, MatterStatus.TO_ASSIGN,
           "Recherche d'antériorité puis dépôt INPI et EUIPO de la nouvelle marque de la collection, classes 20 et 35. "
           "Échanges en italien avec la direction du client.", required_language="Italien")

    # 4. En proposition : le n°1 a refusé, le n°2 (compte Exemple) doit répondre
    audit = matter("2026-045", "Hestia – audit des baux commerciaux", "Groupe Hestia Immobilier", "Droit immobilier",
                   "Baux commerciaux", "audit", 2, 12, deadline(9), EXAMPLE_PARTNER, MatterStatus.PROPOSING,
                   "Revue de 12 baux commerciaux : échéances, clauses d'indexation, répartition des charges, risques "
                   "de déplafonnement. Note de synthèse pour le client.", notified_at=now - timedelta(days=1))
    sent1, sent2 = now - timedelta(hours=5), now - timedelta(hours=2)
    db.add_all([
        Proposal(matter_id=audit.id, user_id=users["Léa Dubois"].id, round=1, rank=1, status=ProposalStatus.REFUSED,
                 sent_at=sent1, expires_at=sent1 + timedelta(hours=4), responded_at=sent2,
                 refusal_reason="overloaded", score=78),
        Proposal(matter_id=audit.id, user_id=users[EXAMPLE_ASSOCIATE].id, round=1, rank=2,
                 status=ProposalStatus.PENDING, sent_at=sent2, expires_at=sent2 + timedelta(hours=4), score=71),
        Proposal(matter_id=audit.id, user_id=users["Sarah Benali"].id, round=1, rank=3, status=ProposalStatus.QUEUED,
                 score=62),
    ])
    event("partner_notified", "Associé notifié (J-10)", None, now - timedelta(days=1), audit)
    event("selection", "Sélection validée par Exemple : Léa Dubois → Exemple → Sarah Benali",
          users[EXAMPLE_PARTNER], sent1, audit)
    event("proposed", "Proposé à Léa Dubois (choix n°1)", None, sent1, audit)
    event("refused", "Refusé par Léa Dubois (Surcharge de travail)", users["Léa Dubois"], sent2, audit)
    event("proposed", "Proposé à Exemple (choix n°2)", None, sent2, audit)

    # 5. Dossier en cours chez le collaborateur Exemple, sans tâche : pour créer sa première tâche en démo
    rent = matter("2026-047", "Hestia – recouvrement de loyers impayés", "Groupe Hestia Immobilier",
                  "Contentieux commercial", "Recouvrement de créances", "conclusions", 2, 12, deadline(10),
                  EXAMPLE_PARTNER, MatterStatus.ACTIVE,
                  "Trois locataires commerciaux en impayé depuis six mois : mises en demeure, commandements de payer "
                  "visant la clause résolutoire, puis assignations en référé si nécessaire.",
                  created_at=now - timedelta(days=2))
    assign(rent, EXAMPLE_ASSOCIATE, now - timedelta(days=1))

    # 6. Dossier en cours chez Julien : une tâche qu'il fait lui-même, une déléguée à un stagiaire
    prud = matter("2026-033", "Lemaire – contentieux prud'homal (faute grave)", "Boulangeries Lemaire", "Droit social",
                  "Contentieux prud'homal", "conclusions", 2, 25, deadline(12), "Antoine Ferrand", MatterStatus.ACTIVE,
                  "Contestation d'un licenciement pour faute grave devant le conseil de prud'hommes.",
                  created_at=now - timedelta(days=9))
    assign(prud, "Julien Moreau", now - timedelta(days=8))
    t1 = task(prud, "Rédiger les conclusions en défense",
              "Conclusions en défense : caractérisation de la faute grave, attestations et pièces.", 10, deadline(4),
              TaskStatus.IN_PROGRESS, "conclusions", created_at=now - timedelta(days=7))
    event("in_progress", f"Julien Moreau réalise « {t1.title} » lui-même / elle-même", users["Julien Moreau"],
          now - timedelta(days=7), prud, t1)
    entries(t1, "Julien Moreau", [
        (5, 3, "analyse dossier et pièces", "Analyse du dossier disciplinaire et des pièces communiquées par le client."),
        (3, 2.5, "plan des conclusions", "Élaboration du plan des conclusions en défense."),
    ], TimeEntryStatus.VALIDATED)
    entries(t1, "Julien Moreau", [(1, 1.5, "début rédaction conclusions",
                                   "Rédaction de la première partie des conclusions en défense.")], TimeEntryStatus.DRAFT)
    db.add(CalendarEvent(user_id=users["Julien Moreau"].id, title="Rédaction conclusions prud'homales", kind="travail",
                         start=_at(today, 14), end=_at(today, 16.5), task_id=t1.id))
    t2 = task(prud, "Recherche de jurisprudence sur la faute grave",
              "Sélectionner la jurisprudence récente de la chambre sociale sur des faits comparables.", 4, deadline(3),
              TaskStatus.IN_PROGRESS, "consultation", delegate="Nathan Roux", created_at=now - timedelta(days=4))
    db.add(Delegation(task_id=t2.id, from_user_id=users["Julien Moreau"].id, to_user_id=users["Nathan Roux"].id,
                      status=DelegationStatus.ACCEPTED, created_at=now - timedelta(days=4),
                      responded_at=now - timedelta(days=4)))
    event("delegated", f"Julien Moreau délègue « {t2.title} » à Nathan Roux", users["Julien Moreau"],
          now - timedelta(days=4), prud, t2)
    event("delegation_accepted", f"Nathan Roux a accepté « {t2.title} »", users["Nathan Roux"],
          now - timedelta(days=4), prud, t2)

    t_ex = task(prud, "Préparer le bordereau de pièces",
                "Lister et numéroter les pièces communiquées (attestations, lettre de licenciement, contrat).", 2,
                deadline(2), TaskStatus.DELEGATION_PENDING, "formalites", created_at=now - timedelta(hours=3))
    db.add(Delegation(task_id=t_ex.id, from_user_id=users["Julien Moreau"].id, to_user_id=users[EXAMPLE_INTERN].id,
                      status=DelegationStatus.PENDING, created_at=now - timedelta(hours=3)))
    event("delegated", f"Julien Moreau délègue « {t_ex.title} » à Exemple", users["Julien Moreau"],
          now - timedelta(hours=3), prud, t_ex)

    # 7. Dossier en cours chez Camille : une tâche terminée (prête à facturer), une déléguée à Chloé
    corpo = matter("2026-021", "Nordis – corporate (augmentation de capital)", "Nordis Industrie", "Droit des sociétés",
                   "Secrétariat juridique et gouvernance", "formalites", 2, 30, deadline(15), "Hélène Marchal",
                   MatterStatus.ACTIVE, "Augmentation de capital, cession de parts d'une filiale et formalités.",
                   created_at=now - timedelta(days=21))
    assign(corpo, "Camille Bernard", now - timedelta(days=20))
    t3 = task(corpo, "Cession de parts sociales d'une filiale",
              "Rédaction de l'acte de cession, garantie d'actif et de passif, formalités.", 9, now - timedelta(days=2),
              TaskStatus.DONE, "acte", sub="Fusions-acquisitions", created_at=now - timedelta(days=19),
              completed_at=now - timedelta(days=3))
    event("done", f"« {t3.title} » terminée par Camille Bernard", users["Camille Bernard"], now - timedelta(days=3),
          corpo, t3)
    entries(t3, "Camille Bernard", [
        (15, 2, "analyse projet de cession", "Analyse du projet de cession et des statuts de la filiale."),
        (12, 3.5, "rédaction acte de cession", "Rédaction de l'acte de cession de parts sociales."),
        (9, 2, "GAP", "Rédaction de la convention de garantie d'actif et de passif."),
        (5, 1.5, "call acheteur + modifs", "Conférence téléphonique avec le conseil de l'acquéreur et intégration "
                                          "des commentaires."),
    ], TimeEntryStatus.VALIDATED)
    t4 = task(corpo, "Mise à jour des statuts et PV d'assemblée",
              "Mettre à jour les statuts, rédiger le procès-verbal d'AGE et préparer les formalités au greffe.", 6,
              deadline(8), TaskStatus.IN_PROGRESS, "formalites", delegate="Chloé Martin",
              created_at=now - timedelta(days=4))
    db.add(Delegation(task_id=t4.id, from_user_id=users["Camille Bernard"].id, to_user_id=users["Chloé Martin"].id,
                      status=DelegationStatus.ACCEPTED, created_at=now - timedelta(days=3),
                      responded_at=now - timedelta(days=3)))
    event("delegated", f"Camille Bernard délègue « {t4.title} » à Chloé Martin", users["Camille Bernard"],
          now - timedelta(days=3, hours=1), corpo, t4)
    event("delegation_accepted", f"Chloé Martin a accepté « {t4.title} »", users["Chloé Martin"],
          now - timedelta(days=3), corpo, t4)
    entries(t4, "Chloé Martin", [(2, 2, "projet de PV d'AGE",
                                  "Rédaction du projet de procès-verbal d'assemblée générale extraordinaire.")],
            TimeEntryStatus.VALIDATED)
    t_reg = task(corpo, "Mettre à jour le registre des mouvements de titres",
                 "Reporter la cession et l'augmentation de capital dans le registre.", 2, deadline(6),
                 TaskStatus.IN_PROGRESS, "formalites", delegate=EXAMPLE_INTERN, created_at=now - timedelta(days=2))
    db.add(Delegation(task_id=t_reg.id, from_user_id=users["Camille Bernard"].id, to_user_id=users[EXAMPLE_INTERN].id,
                      status=DelegationStatus.ACCEPTED, created_at=now - timedelta(days=2),
                      responded_at=now - timedelta(days=2)))
    event("delegated", f"Camille Bernard délègue « {t_reg.title} » à Exemple", users["Camille Bernard"],
          now - timedelta(days=2), corpo, t_reg)
    db.add(CalendarEvent(user_id=users["Chloé Martin"].id, title="Mise à jour des statuts Nordis", kind="travail",
                         start=_at(today, 10), end=_at(today, 12), task_id=t4.id))

    # 8. Dossier en cours chez Maxime : une note terminée, prête à facturer
    celtis = matter("2026-051", "Celtis – enquête interne", "Transports Celtis", "Droit pénal des affaires",
                    "Abus de biens sociaux et fraude", "consultation", 3, 20, deadline(20), "Antoine Ferrand",
                    MatterStatus.ACTIVE, "Enquête interne sur des flux suspects entre la société et la holding du "
                    "dirigeant.", created_at=now - timedelta(days=16))
    assign(celtis, "Maxime Fontaine", now - timedelta(days=15))
    t5 = task(celtis, "Note sur le risque d'abus de biens sociaux",
              "Analyser les flux entre la société et la holding du dirigeant et qualifier le risque pénal.", 8,
              now - timedelta(days=4), TaskStatus.DONE, "consultation", created_at=now - timedelta(days=14),
              completed_at=now - timedelta(days=5))
    event("done", f"« {t5.title} » terminée par Maxime Fontaine", users["Maxime Fontaine"], now - timedelta(days=5),
          celtis, t5)
    entries(t5, "Maxime Fontaine", [
        (12, 3, "analyse flux financiers", "Analyse des flux financiers entre la société et la holding du dirigeant."),
        (8, 4, "rédaction note", "Rédaction d'une note d'analyse du risque pénal au regard de l'abus de biens sociaux."),
    ], TimeEntryStatus.VALIDATED)

    db.flush()

    from app.services.notifications import notify
    pending = next(p for p in audit.proposals if p.status == ProposalStatus.PENDING)
    notify(db, users[EXAMPLE_ASSOCIATE], "proposal",
           f"Exemple vous propose le dossier « {audit.name} » (échéance {audit.deadline:%d/%m à %Hh%M}). "
           f"Réponse attendue avant le {pending.expires_at:%d/%m à %Hh%M}.", audit)

    notify(db, users[EXAMPLE_INTERN], "delegation",
           f"Julien Moreau vous confie « {t_ex.title} » (deadline {t_ex.deadline:%d/%m à %Hh%M}).", t_ex)

    from app.services.scheduler import tick
    tick(db)


if __name__ == "__main__":
    reset_database()
    print("Base de démo réinitialisée.")
