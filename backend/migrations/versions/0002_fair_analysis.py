"""evaluations: FAIR agent report and headline figures

Adds the FAIR agent's output to each evaluation (ADR 0005): the full report
with its investigation trace, and the expected annual loss and chance of
reaching the client's tolerance for the history list. All nullable: older
evaluations and evaluations with FAIR switched off have none.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("evaluations") as batch:
        batch.add_column(
            sa.Column(
                "fair_analysis",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("fair_ale_mean", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column("fair_prob_exceeds_tolerance", sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("evaluations") as batch:
        batch.drop_column("fair_prob_exceeds_tolerance")
        batch.drop_column("fair_ale_mean")
        batch.drop_column("fair_analysis")
