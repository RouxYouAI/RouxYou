from abc import ABC, abstractmethod
from typing import Dict, List, Any

class Capability(ABC):
    """Base interface for all Worker capabilities."""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def dependencies(self) -> List[str]:
        return []

    @abstractmethod
    def run(self, **kwargs) -> Dict[str, Any]:
        pass
