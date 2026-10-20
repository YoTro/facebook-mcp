"""Route registry (L2). Pure data describing Facebook endpoints."""

from . import graphql, rest
from .base import FACEBOOK_BASE, GRAPHQL_PATH, Route

__all__ = ["FACEBOOK_BASE", "GRAPHQL_PATH", "Route", "graphql", "rest"]
