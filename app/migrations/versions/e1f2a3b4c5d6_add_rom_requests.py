"""Add rom_requests table

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'rom_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('username', sa.String(length=100), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('admin_note', sa.String(length=500), nullable=True),
        sa.Column('rom_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_rom_requests_user_id', 'rom_requests', ['user_id'])
    op.create_index('ix_rom_requests_status', 'rom_requests', ['status'])


def downgrade():
    op.drop_index('ix_rom_requests_status', table_name='rom_requests')
    op.drop_index('ix_rom_requests_user_id', table_name='rom_requests')
    op.drop_table('rom_requests')
