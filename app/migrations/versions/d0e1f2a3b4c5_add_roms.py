"""Add roms table for the retro ROM library

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd0e1f2a3b4c5'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'roms',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=512), nullable=False),
        sa.Column('relpath', sa.String(length=1024), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('added_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('relpath'),
    )
    op.create_index('ix_roms_platform', 'roms', ['platform'])


def downgrade():
    op.drop_index('ix_roms_platform', table_name='roms')
    op.drop_table('roms')
