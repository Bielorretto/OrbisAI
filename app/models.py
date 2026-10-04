"""Modèle de données. Code en anglais, libellés d'interface en français (voir labels.py)."""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role:
    """Hiérarchie du cabinet : associé > collaborateur (senior, collaborateur, junior) > stagiaire."""
    PARTNER = "associe"
    SENIOR = "collab_senior"
    ASSOCIATE = "collaborateur"
    JUNIOR = "junior"
    INTERN = "stagiaire"

    ASSIGNERS = (PARTNER,)                        # responsables des dossiers (et du chiffre d'affaires)
    LAWYERS = (SENIOR, ASSOCIATE, JUNIOR)          # collaborateurs : staffés sur les dossiers, créent les tâches
    LEVEL = {INTERN: 1, JUNIOR: 2, ASSOCIATE: 3, SENIOR: 4, PARTNER: 5}


class TeamRole:
    """Place d'une personne dans l'équipe d'un dossier."""
    PARTNER = "associe"
    LAWYER = "collaborateur"
    INTERN = "stagiaire"


class MatterStatus:
    """Cycle de vie d'un dossier : l'associé l'attribue à un collaborateur (cascade 1 -> 2 -> 3)."""
    TO_ASSIGN = "to_assign"            # en attente d'attribution par l'associé
    PROPOSING = "proposing"            # cascade en cours
    CASCADE_FAILED = "cascade_failed"  # les collaborateurs choisis ont tous refusé : retour à l'associé
    ACTIVE = "active"                  # accepté : le collaborateur responsable crée et gère les tâches
    CLOSED = "closed"

    UNASSIGNED = (TO_ASSIGN, PROPOSING, CASCADE_FAILED)
    OPEN = (TO_ASSIGN, PROPOSING, CASCADE_FAILED, ACTIVE)


class TaskStatus:
    """Cycle de vie d'une tâche, créée par le collaborateur responsable dans un de ses dossiers."""
    ACCEPTED = "accepted"                       # à lancer : le collaborateur choisit de la faire ou de la déléguer
    DELEGATION_PENDING = "delegation_pending"   # proposée à un stagiaire
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"                     # travail du stagiaire à valider par le collaborateur
    DONE = "done"

    OPEN = (ACCEPTED, DELEGATION_PENDING, IN_PROGRESS, IN_REVIEW)
    ACTIVE_WORK = OPEN


class ProposalStatus:
    QUEUED = "queued"        # dans la file, pas encore envoyée
    PENDING = "pending"      # envoyée, en attente de réponse
    ACCEPTED = "accepted"
    REFUSED = "refused"
    EXPIRED = "expired"      # pas de réponse dans le délai
    CANCELLED = "cancelled"  # un rang précédent a accepté


class DelegationStatus:
    PENDING = "pending"
    ACCEPTED = "accepted"
    REFUSED = "refused"


class TimeEntryStatus:
    DRAFT = "draft"
    VALIDATED = "validated"
    INVOICED = "invoiced"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    role: Mapped[str] = mapped_column(String(20), index=True)
    bar_year: Mapped[int | None] = mapped_column(Integer)  # année de prestation de serment
    languages: Mapped[str] = mapped_column(String(200), default="Français")
    weekly_capacity_hours: Mapped[float] = mapped_column(Float, default=40)
    hourly_rate: Mapped[float] = mapped_column(Float, default=0)
    bio: Mapped[str] = mapped_column(Text, default="")
    joined_year: Mapped[int | None] = mapped_column(Integer)          # arrivée au cabinet
    # Listes séparées par des virgules (prototype)
    sectors: Mapped[str] = mapped_column(String(300), default="")         # secteurs d'activité clients maîtrisés
    countries: Mapped[str] = mapped_column(String(300), default="France")
    legal_systems: Mapped[str] = mapped_column(String(300), default="Droit français")
    jurisdictions: Mapped[str] = mapped_column(String(300), default="")   # barreaux / juridictions où il intervient
    preferences: Mapped[str] = mapped_column(String(200), default="")     # recherche, rédaction, négociation, contentieux
    litigation_level: Mapped[int] = mapped_column(Integer, default=0)     # expérience contentieuse 0..3
    transactional_level: Mapped[int] = mapped_column(Integer, default=0)  # expérience transactionnelle 0..3
    international_level: Mapped[int] = mapped_column(Integer, default=0)  # expérience internationale 0..2
    task_type_counts: Mapped[str] = mapped_column(Text, default="{}")     # JSON {type de dossier: nombre traité}
    profile_embedding: Mapped[str | None] = mapped_column(Text)  # JSON {"model":..., "v": [...]}

    specialties: Mapped[list["UserSpecialty"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    sub_specialties: Mapped[list["UserSubSpecialty"]] = relationship(back_populates="user",
                                                                     cascade="all, delete-orphan")

    def years_at_bar(self, today: date) -> int:
        return max(0, today.year - self.bar_year) if self.bar_year else 0

    def years_at_firm(self, today: date) -> int:
        return max(0, today.year - self.joined_year) if self.joined_year else 0

    @property
    def is_assigner(self) -> bool:
        return self.role in Role.ASSIGNERS

    @property
    def is_lawyer(self) -> bool:
        return self.role in Role.LAWYERS

    @property
    def level(self) -> int:
        return Role.LEVEL.get(self.role, 0)


class Specialty(Base):
    __tablename__ = "specialties"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class UserSpecialty(Base):
    __tablename__ = "user_specialties"
    __table_args__ = (UniqueConstraint("user_id", "specialty_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    specialty_id: Mapped[int] = mapped_column(ForeignKey("specialties.id"))
    level: Mapped[int] = mapped_column(Integer, default=1)  # 1 = notions, 2 = confirmé, 3 = expert
    cases_count: Mapped[int] = mapped_column(Integer, default=0)  # dossiers traités dans ce domaine (historique)

    user: Mapped[User] = relationship(back_populates="specialties")
    specialty: Mapped[Specialty] = relationship()


class SubSpecialty(Base):
    """Sous-spécialité d'un domaine du droit (ex. Contentieux commercial > Rupture brutale)."""
    __tablename__ = "sub_specialties"

    id: Mapped[int] = mapped_column(primary_key=True)
    specialty_id: Mapped[int] = mapped_column(ForeignKey("specialties.id"))
    name: Mapped[str] = mapped_column(String(160))

    specialty: Mapped[Specialty] = relationship()


class UserSubSpecialty(Base):
    __tablename__ = "user_sub_specialties"
    __table_args__ = (UniqueConstraint("user_id", "sub_specialty_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    sub_specialty_id: Mapped[int] = mapped_column(ForeignKey("sub_specialties.id"))
    level: Mapped[int] = mapped_column(Integer, default=1)
    cases_count: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(back_populates="sub_specialties")
    sub_specialty: Mapped[SubSpecialty] = relationship()


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    sector: Mapped[str] = mapped_column(String(120), default="")      # secteur d'activité
    language: Mapped[str] = mapped_column(String(60), default="Français")  # langue maternelle du client
    country: Mapped[str] = mapped_column(String(80), default="France")


class Matter(Base):
    """Dossier : c'est l'unité que l'associé attribue à un collaborateur."""
    __tablename__ = "matters"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    fee_mode: Mapped[str] = mapped_column(String(20), default="hourly")  # prototype : horaire uniquement
    # Caractéristiques utilisées par le matching (voir services/scoring.py)
    specialty_id: Mapped[int | None] = mapped_column(ForeignKey("specialties.id"))
    sub_specialty_id: Mapped[int | None] = mapped_column(ForeignKey("sub_specialties.id"))
    task_type: Mapped[str | None] = mapped_column(String(40))         # type de dossier, voir labels.TASK_TYPES
    min_level: Mapped[str] = mapped_column(String(20), default=Role.JUNIOR)
    required_language: Mapped[str | None] = mapped_column(String(60))
    required_jurisdiction: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(80))
    legal_system: Mapped[str | None] = mapped_column(String(80))
    complexity: Mapped[int] = mapped_column(Integer, default=2)
    estimated_hours: Mapped[float] = mapped_column(Float, default=10)  # charge estimée du dossier
    deadline: Mapped[datetime] = mapped_column(DateTime, index=True)   # prochaine échéance du dossier
    notify_days_before: Mapped[int] = mapped_column(Integer, default=7)
    priority: Mapped[int | None] = mapped_column(Integer)                 # rang de priorité (1 = le plus prioritaire)
    # Attribution
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))    # associé qui a ouvert le dossier
    status: Mapped[str] = mapped_column(String(30), default=MatterStatus.TO_ASSIGN, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)       # notification J-x envoyée
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    budget_amount: Mapped[float | None] = mapped_column(Float)           # charge estimée x taux du responsable
    ai_summary: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[str | None] = mapped_column(Text)

    client: Mapped[Client] = relationship()
    specialty: Mapped["Specialty | None"] = relationship()
    sub_specialty: Mapped["SubSpecialty | None"] = relationship()
    created_by: Mapped["User"] = relationship(foreign_keys=[created_by_id])
    members: Mapped[list["MatterMember"]] = relationship(back_populates="matter", order_by="MatterMember.id",
                                                         cascade="all, delete-orphan")
    proposals: Mapped[list["Proposal"]] = relationship(
        back_populates="matter", order_by="(Proposal.round, Proposal.rank)", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="matter", order_by="Task.deadline")
    events: Mapped[list["TaskEvent"]] = relationship(order_by="TaskEvent.id", viewonly=True)

    @property
    def title(self) -> str:
        return f"{self.reference} · {self.name}"

    def team(self, team_role: str) -> list["User"]:
        return [m.user for m in self.members if m.team_role == team_role]

    @property
    def partners(self) -> list["User"]:
        return self.team(TeamRole.PARTNER)

    @property
    def lawyers(self) -> list["User"]:
        return self.team(TeamRole.LAWYER)

    @property
    def interns(self) -> list["User"]:
        return self.team(TeamRole.INTERN)

    def has_member(self, user: "User", team_role: str | None = None) -> bool:
        return any(m.user_id == user.id and (team_role is None or m.team_role == team_role) for m in self.members)


class MatterMember(Base):
    """Équipe d'un dossier : plusieurs associés, collaborateurs et stagiaires peuvent y travailler."""
    __tablename__ = "matter_members"
    __table_args__ = (UniqueConstraint("matter_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    matter_id: Mapped[int] = mapped_column(ForeignKey("matters.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    team_role: Mapped[str] = mapped_column(String(20))
    joined_at: Mapped[datetime] = mapped_column(DateTime)

    matter: Mapped[Matter] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()


class Conflict(Base):
    """Conflit d'intérêts : la personne ne peut pas travailler pour ce client."""
    __tablename__ = "conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    reason: Mapped[str] = mapped_column(String(300), default="")

    client: Mapped[Client] = relationship()


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # audience | rdv | conge | travail | formation
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime] = mapped_column(DateTime)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))


class Task(Base):
    """Tâche créée par le collaborateur responsable d'un dossier, qu'il réalise ou délègue à un stagiaire."""
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    matter_id: Mapped[int] = mapped_column(ForeignKey("matters.id"))
    specialty_id: Mapped[int | None] = mapped_column(ForeignKey("specialties.id"))
    sub_specialty_id: Mapped[int | None] = mapped_column(ForeignKey("sub_specialties.id"))
    task_type: Mapped[str | None] = mapped_column(String(40))         # voir labels.TASK_TYPES
    min_level: Mapped[str] = mapped_column(String(20), default=Role.INTERN)  # « collaborateur » = pas de délégation
    required_language: Mapped[str | None] = mapped_column(String(60))
    required_jurisdiction: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(80))           # par défaut : pays du client
    legal_system: Mapped[str | None] = mapped_column(String(80))      # par défaut : droit français
    complexity: Mapped[int] = mapped_column(Integer, default=2)  # 1..3
    estimated_hours: Mapped[float] = mapped_column(Float, default=4)
    deadline: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))       # collaborateur qui l'a créée
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.ACCEPTED, index=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))   # collaborateur responsable
    delegate_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))   # stagiaire qui exécute
    created_at: Mapped[datetime] = mapped_column(DateTime)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)       # rappel avant deadline envoyé
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    budget_amount: Mapped[float | None] = mapped_column(Float)                # effort estimé x taux du responsable
    budget_alert_level: Mapped[int] = mapped_column(Integer, default=0)       # dernier seuil d'alerte franchi
    ai_summary: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[str | None] = mapped_column(Text)

    matter: Mapped[Matter] = relationship(back_populates="tasks")
    specialty: Mapped[Specialty | None] = relationship()
    sub_specialty: Mapped[SubSpecialty | None] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    delegate: Mapped[User | None] = relationship(foreign_keys=[delegate_id])
    events: Mapped[list["TaskEvent"]] = relationship(order_by="TaskEvent.id", viewonly=True)
    delegations: Mapped[list["Delegation"]] = relationship(back_populates="task", order_by="Delegation.id")

    @property
    def worker(self) -> User | None:
        """La personne qui exécute réellement la tâche."""
        return self.delegate or self.assignee

    @property
    def client(self) -> Client:
        return self.matter.client


class Proposal(Base):
    """Une ligne par collaborateur choisi par l'associé pour un dossier : c'est le cœur de la cascade."""
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    matter_id: Mapped[int] = mapped_column(ForeignKey("matters.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    round: Mapped[int] = mapped_column(Integer, default=1)  # nouvelle sélection après un échec = round suivant
    rank: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=ProposalStatus.QUEUED)
    score: Mapped[float | None] = mapped_column(Float)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime)
    refusal_reason: Mapped[str | None] = mapped_column(String(40))
    refusal_comment: Mapped[str | None] = mapped_column(Text)

    matter: Mapped[Matter] = relationship(back_populates="proposals")
    user: Mapped[User] = relationship()


class Delegation(Base):
    __tablename__ = "delegations"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default=DelegationStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime)
    refusal_reason: Mapped[str | None] = mapped_column(String(40))

    task: Mapped[Task] = relationship(back_populates="delegations")
    from_user: Mapped[User] = relationship(foreign_keys=[from_user_id])
    to_user: Mapped[User] = relationship(foreign_keys=[to_user_id])


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(String(300), default="/")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class EmailOutbox(Base):
    """Emails envoyés (ou simulés si aucun SMTP n'est configuré)."""
    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    to: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class TaskEvent(Base):
    """Historique : base de la frise chronologique du suivi. Un événement de tâche est aussi rattaché à son dossier,
    pour que la frise du dossier montre tout ce qui s'y passe."""
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    matter_id: Mapped[int | None] = mapped_column(ForeignKey("matters.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    actor: Mapped[User | None] = relationship()


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date)
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)  # début du chronomètre
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")      # note rapide de l'avocat
    label: Mapped[str] = mapped_column(Text, default="")     # libellé de facturation (rédigé par Mistral, éditable)
    billable: Mapped[bool] = mapped_column(Boolean, default=True)
    rate: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default=TimeEntryStatus.DRAFT)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship()
    task: Mapped[Task] = relationship()

    @property
    def hours(self) -> float:
        return round(self.minutes / 60, 2)

    @property
    def amount(self) -> float:
        return round(self.hours * self.rate, 2) if self.billable else 0.0


class PreInvoice(Base):
    __tablename__ = "pre_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    matter_id: Mapped[int] = mapped_column(ForeignKey("matters.id"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | validated
    created_at: Mapped[datetime] = mapped_column(DateTime)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime)

    matter: Mapped[Matter] = relationship()
    lines: Mapped[list["PreInvoiceLine"]] = relationship(
        back_populates="pre_invoice", order_by="PreInvoiceLine.id", cascade="all, delete-orphan")

    @property
    def total(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    @property
    def total_before_write_off(self) -> float:
        return round(sum(line.gross_amount for line in self.lines), 2)


class PreInvoiceLine(Base):
    __tablename__ = "pre_invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    pre_invoice_id: Mapped[int] = mapped_column(ForeignKey("pre_invoices.id"))
    time_entry_id: Mapped[int] = mapped_column(ForeignKey("time_entries.id"))
    work_date: Mapped[date] = mapped_column(Date)
    user_name: Mapped[str] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(Text)
    hours: Mapped[float] = mapped_column(Float)
    rate: Mapped[float] = mapped_column(Float)
    write_off_pct: Mapped[float] = mapped_column(Float, default=0)  # réduction accordée par l'associé

    pre_invoice: Mapped[PreInvoice] = relationship(back_populates="lines")

    @property
    def gross_amount(self) -> float:
        return round(self.hours * self.rate, 2)

    @property
    def amount(self) -> float:
        return round(self.gross_amount * (1 - self.write_off_pct / 100), 2)


class Setting(Base):
    """Petits réglages persistants (ex. décalage de l'horloge de démo)."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class RankingCache(Base):
    """Classement établi par Mistral, mémorisé : le classement par l'IA n'est pas déterministe, on le garde stable
    tant que les informations utilisées (dossier, candidats, charges) n'ont pas changé."""
    __tablename__ = "ranking_cache"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)   # ex. matter:12:collaborateur
    fingerprint: Mapped[str] = mapped_column(String(64))              # empreinte des informations utilisées
    payload: Mapped[str] = mapped_column(Text)                        # JSON {order, scores, reasons, source}
    created_at: Mapped[datetime] = mapped_column(DateTime)
