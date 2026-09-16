"""Native daily owner graph declared before opening any storage or resource.

Generation, media and semantic work share one Provider repository. Content,
configuration, formal information, import, scheduling and reasoning use their
own public transaction participants on that same persistence service.
"""
from collections.abc import Callable
from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
from companion_memory.media.service import MediaService
from companion_memory.provider.ledger import LedgerAssembly
from companion_memory.provider.daily_commands import DailyProviderCommands
from companion_memory.persistence import PersistenceService,UnitOfWork
from companion_memory.persistence.semantic_commands import SemanticCommands
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.text_records import extend_catalog
from companion_memory.memory.initial_self_commands import InitialSelfCommands
from companion_memory.memory.initial_subjects import definition as subjects_definition,register_subjects
from companion_memory.retrieval.repository import retrieval_catalog
from companion_memory.retrieval.semantic_repository import semantic_retrieval_catalog
from companion_memory.retrieval.semantic_work import SemanticWork
from companion_memory.retrieval.semantic_generation import SemanticGenerations
from companion_memory.retrieval.semantic_cache import SemanticQueryCache
from companion_memory.provider.embedding_service import EmbeddingProvider
from companion_memory.cognition.fixed_memory import FixedMemorySets
from companion_memory.cognition.daily_reasoning import DailyReasoning
from companion_memory.cognition.daily_material_storage import DailyMaterialStorage
from companion_memory.goals.repository import goals_catalog
from companion_memory.goals.semantic_decisions import GoalComparisons
from companion_memory.state.repository import state_catalog
from companion_memory.information.initialization import InformationInitialization
from companion_memory.information.management import ManagementAssembly
from companion_memory.self_model.approved_import import ApprovedPersonaImport
from .content_assembly import ContentAssembly
from .candidate_goals import add_goal_commands
from .daily_schedule import DailySchedule
from .daily_initialization import DailyInitialization
from .daily_application import DailyApplication
from .daily_persona_mode import DailyPersonaMode
from companion_memory.self_model.daily_persona import DailyPersona
from companion_memory.media.daily_work import DailyImageWork

class DailyAssembly:
    """One static storage graph; only the host may install native owner services."""
    def __init__(self):
        self.media=MediaService(daily_format=True)
        self.content=ContentAssembly(self.media,self.media.repositories,information_format=True,daily_format=True)
        self.retrieval_catalog=extend_catalog(retrieval_catalog(semantic_format=True),semantic_retrieval_catalog(),5)
        self.information_catalogs=(self.retrieval_catalog,state_catalog(),goals_catalog(daily_format=True))
        catalogs={c.definition.owner_module:c for c in self.content.catalogs}
        self.configuration=DailyConfigurationAssembly(catalogs['memory'],self.retrieval_catalog)
        self.ledger=LedgerAssembly(daily_format=True)
        self.persona=ApprovedPersonaImport(catalogs['memory'])
        self.materials=DailyMaterialStorage(catalogs['cognition'])
        self.schedule=DailySchedule(catalogs['runtime'],self.content)
        self.initialization=DailyInitialization(catalogs['runtime'])
        self.goal_comparisons=GoalComparisons(self.information_catalogs[2],self.materials,self.ledger.repository.definition)
        add_goal_commands(self.content,self.information_catalogs[2].definition)
        domains=tuple(c.definition for c in self.information_catalogs)
        participants=self.content.repositories+domains
        self.initializer=InformationInitialization(participants,self.configuration.repository.definition)
        self.management=ManagementAssembly(participants)
        self.initial_self=InitialSelfCommands(catalogs['memory'])
        self.initial_subjects=subjects_definition((catalogs['memory'].definition,),self._subjects)
        self.repositories=(self.configuration.repository.definition,)+self.content.repositories+self.ledger.repositories+domains+self.persona.repositories
        self.persona_mode=DailyPersonaMode(self.content,self.repositories)
        self.initial_persona=DailyPersona(self.persona.catalog,self.materials,self.persona_mode,self.repositories)
        self.content.replace_static_command('change_content_mode',self.persona_mode.definition)
        self.daily_provider=DailyProviderCommands(self.ledger.repository.definition,tuple(repository for repository in self.repositories if repository.owner_module in ('cognition','media','goals','self_model')))
        self.semantic=SemanticCommands(self.repositories,self._semantic,usage_only=True)
        self.reasoning=DailyReasoning(catalogs['cognition'],self.materials,self.repositories)
        self.application=DailyApplication(self.content,self.reasoning,self.repositories)
        self.image_work=DailyImageWork(self.media,self.content,self.ledger.repository.definition)
        self.content.daily_input_failures=self.image_work
        groups=(self.initialization.commands,self.configuration.commands,self.configuration.root_commands,self.content.commands,self.ledger.commands,self.media.commands,self.initializer.commands,
            self.management.commands,self.initial_self.commands,(self.initial_subjects,),self.semantic.commands,self.persona.commands,self.initial_persona.commands,
            self.materials.commands,self.schedule.commands,self.daily_provider.commands,self.goal_comparisons.commands,self.reasoning.commands,self.application.commands,self.image_work.commands)
        self.commands=tuple(definition for group in groups for definition in group)
        self.storage=PersistenceService(self.repositories,self.commands,assembly_format='DAILY_COGNITION_V1')
        self.work:SemanticWork|None=None;self.generations:SemanticGenerations|None=None;self.cache:SemanticQueryCache|None=None
        self.embedding:EmbeddingProvider|None=None;self.fixed:FixedMemorySets|None=None
        self.subject_origin:str|None=None
        self.admit_normal:Callable[[UnitOfWork],None]|None=None

    def _subjects(self,uow:UnitOfWork,values:Record) -> object:
        if self.subject_origin is None or self.work is None or self.admit_normal is None:
            raise ValueError('Native initial subject authority is not bound.')
        self.admit_normal(uow)
        return register_subjects(self.content.memory,uow,values,self.subject_origin,self.content.utc_now_us())

    def _semantic(self,kind:str,uow:UnitOfWork,envelope:Record,payload:Record) -> object:
        if kind.startswith('fixed_') or kind in ('bind','resume','begin_generation','append_page','confirm_page','seal_generation','publish_generation') or kind=='prepare' and payload['kind']=='EMBED':
            if self.admit_normal is None:raise ValueError('Native daily mode admission is not bound.')
            self.admit_normal(uow)
        if kind.startswith('fixed_'):owner=self.fixed
        elif kind in ('store_embedding_handoff','confirm_embedding_handoff','retire_embedding_handoff'):owner=self.embedding
        elif kind in ('begin_generation','append_page','confirm_page','seal_generation','publish_generation','retire_page','generation_fail'):owner=self.generations
        elif kind in ('cache_bind','cache_expire','gc_page'):owner=self.cache
        else:owner=self.work
        if owner is None:raise ValueError('Native daily semantic owner is not bound.')
        return owner.handle(kind,uow,envelope,payload)
