"""Libellés d'interface (français) pour les valeurs stockées en base."""
from app.models import DelegationStatus, MatterStatus, ProposalStatus, Role, TaskStatus, TimeEntryStatus

ROLE = {Role.PARTNER: "Associé", Role.SENIOR: "Collab. senior",
        Role.ASSOCIATE: "Collaborateur", Role.JUNIOR: "Junior", Role.INTERN: "Stagiaire"}

MATTER_STATUS = {
    MatterStatus.TO_ASSIGN: "À attribuer",
    MatterStatus.PROPOSING: "En proposition",
    MatterStatus.CASCADE_FAILED: "Refusé par tous",
    MatterStatus.ACTIVE: "En cours",
    MatterStatus.CLOSED: "Clôturé",
}

TASK_STATUS = {
    TaskStatus.ACCEPTED: "À lancer",
    TaskStatus.DELEGATION_PENDING: "Délégation en attente",
    TaskStatus.IN_PROGRESS: "En cours",
    TaskStatus.IN_REVIEW: "En revue",
    TaskStatus.DONE: "Terminée",
}

PROPOSAL_STATUS = {
    ProposalStatus.QUEUED: "En file",
    ProposalStatus.PENDING: "En attente de réponse",
    ProposalStatus.ACCEPTED: "Acceptée",
    ProposalStatus.REFUSED: "Refusée",
    ProposalStatus.EXPIRED: "Expirée (sans réponse)",
    ProposalStatus.CANCELLED: "Annulée",
}

DELEGATION_STATUS = {
    DelegationStatus.PENDING: "En attente",
    DelegationStatus.ACCEPTED: "Acceptée",
    DelegationStatus.REFUSED: "Refusée",
}

TIME_STATUS = {
    TimeEntryStatus.DRAFT: "Brouillon",
    TimeEntryStatus.VALIDATED: "Validé",
    TimeEntryStatus.INVOICED: "Facturé",
}

REFUSAL_REASONS = {
    "overloaded": "Surcharge de travail",
    "out_of_scope": "Hors de ma spécialité",
    "conflict": "Conflit d'intérêts",
    "absent": "Absent / indisponible",
    "other": "Autre",
}

EVENT_KIND = {
    "audience": "Audience",
    "rdv": "Rendez-vous",
    "conge": "Congé",
    "travail": "Travail sur dossier",
    "formation": "Formation",
}

COMPLEXITY = {1: "Simple", 2: "Intermédiaire", 3: "Complexe"}
SPECIALTY_LEVEL = {1: "Notions", 2: "Confirmé", 3: "Expert"}


def refusal(code: str | None) -> str:
    return REFUSAL_REASONS.get(code or "", code or "")


# Types de dossier : libellé, activités associées (préférences personnelles), nature
TASK_TYPES = {
    # Types de dossier (tableau des dossiers du cabinet)
    "transactionnel": ("Transactionnel", {"négociation", "rédaction"}, "transactionnel"),
    "contentieux": ("Contentieux", {"contentieux", "rédaction"}, "contentieux"),
    "conseil": ("Conseil", {"recherche", "rédaction"}, None),
    # Types de travail (tâches)
    "conclusions": ("Procédure / conclusions", {"contentieux", "rédaction"}, "contentieux"),
    "acte": ("Rédaction d'actes / contrats", {"rédaction"}, "transactionnel"),
    "audit": ("Audit / due diligence", {"recherche"}, "transactionnel"),
    "consultation": ("Consultation / note juridique", {"recherche", "rédaction"}, None),
    "negociation": ("Négociation", {"négociation"}, "transactionnel"),
    "formalites": ("Formalités / secrétariat juridique", {"rédaction"}, None),
}

PREFERENCES = ["recherche", "rédaction", "négociation", "contentieux"]

# Critères d'attribution (document « Critères d'attribution », dans l'ordre hiérarchique à respecter)
ELIMINATORY = {
    "conflict": "Conflit d'intérêts",
    "domain": "Domaine du droit non maîtrisé",
    "sub_specialty": "Sous-spécialité non maîtrisée",
    "hierarchy": "Niveau hiérarchique insuffisant",
    "availability": "Disponibilité minimale requise",
    "language": "Langue obligatoire non maîtrisée",
    "jurisdiction": "Juridiction obligatoire non maîtrisée",
}

MAIN_CRITERIA = {
    "domain_level": "Niveau d'expertise dans le domaine",
    "sub_level": "Niveau d'expertise dans la sous-spécialité",
    "similar_cases": "Nombre de dossiers similaires traités",
    "task_type": "Expérience du type de dossier",
    "workload": "Charge de travail actuelle",
    "open_files": "Nombre de dossiers en cours",
    "deadlines": "Nombre et criticité des échéances à venir",
    "sector": "Secteur d'activité du client maîtrisé",
    "client_knowledge": "Connaissance préalable du client",
    "client_history": "Historique de relation avec le client",
    "client_language": "Langue maternelle du client",
}

COMPLEMENTARY_CRITERIA = {
    "international": "Expérience internationale",
    "country": "Maîtrise du pays concerné",
    "legal_system": "Maîtrise du système juridique concerné",
    "litigation": "Expérience contentieuse",
    "transactional": "Expérience transactionnelle",
    "firm_seniority": "Ancienneté au cabinet",
    "preferences": "Préférences personnelles",
}
