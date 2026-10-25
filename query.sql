CREATE TABLE departments (
    department_index SERIAL PRIMARY KEY,
    department_name  VARCHAR NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE employees (
    employee_index     SERIAL PRIMARY KEY,
    employee_id        VARCHAR NOT NULL UNIQUE,
    department_index   INTEGER REFERENCES departments (department_index) ON DELETE RESTRICT,
    user_password_hash VARCHAR NOT NULL DEFAULT ('!' || gen_random_uuid()::text),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_employees_department_index ON employees (department_index);

CREATE TABLE line_users (
    line_user_index SERIAL PRIMARY KEY,
    line_user_id    VARCHAR NOT NULL UNIQUE,
    employee_index  INTEGER NOT NULL REFERENCES employees (employee_index) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_line_users_employee_index ON line_users (employee_index);

CREATE TABLE admins (
    admin_index    SERIAL PRIMARY KEY,
    employee_index INTEGER NOT NULL UNIQUE REFERENCES employees (employee_index) ON DELETE RESTRICT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE meter_report_order_statuses (
    meter_report_order_status_index      SERIAL PRIMARY KEY,
    meter_report_order_status_name       VARCHAR NOT NULL UNIQUE,
    meter_report_order_status_is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE meter_report_orders (
    meter_report_order_index        SERIAL PRIMARY KEY,
    employee_index                  INTEGER NOT NULL REFERENCES employees (employee_index) ON DELETE RESTRICT,
    meter_id                        VARCHAR NOT NULL,
    project_name                    VARCHAR,
    issue_detail                    TEXT,
    meter_report_order_status_index INTEGER NOT NULL
        REFERENCES meter_report_order_statuses (meter_report_order_status_index) ON DELETE RESTRICT,
    admin_index                     INTEGER REFERENCES admins (admin_index) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_meter_report_orders_employee_index ON meter_report_orders (employee_index);
CREATE INDEX ix_meter_report_orders_meter_id ON meter_report_orders (meter_id);
CREATE INDEX ix_meter_report_orders_meter_report_order_status_index ON meter_report_orders (meter_report_order_status_index);
CREATE INDEX ix_meter_report_orders_admin_index ON meter_report_orders (admin_index);

CREATE TABLE meter_report_order_comments (
    meter_report_order_comment_index SERIAL PRIMARY KEY,
    meter_report_order_index INTEGER NOT NULL
        REFERENCES meter_report_orders (meter_report_order_index) ON DELETE RESTRICT,
    employee_index           INTEGER NOT NULL REFERENCES employees (employee_index) ON DELETE RESTRICT,
    comment_detail           TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_meter_report_order_comments_meter_report_order_index ON meter_report_order_comments (meter_report_order_index);
CREATE INDEX ix_meter_report_order_comments_employee_index ON meter_report_order_comments (employee_index);

CREATE TABLE asset_units (
    asset_unit_index SERIAL PRIMARY KEY,
    asset_unit_name  VARCHAR NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE assets (
    asset_index      SERIAL PRIMARY KEY,
    asset_id         VARCHAR NOT NULL UNIQUE,
    asset_name       VARCHAR NOT NULL,
    asset_unit_index INTEGER NOT NULL REFERENCES asset_units (asset_unit_index) ON DELETE RESTRICT,
    asset_price      NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_assets_asset_unit_index ON assets (asset_unit_index);

CREATE TABLE asset_requisition_order_statuses (
    asset_requisition_order_status_index      SERIAL PRIMARY KEY,
    asset_requisition_order_status_name       VARCHAR NOT NULL UNIQUE,
    asset_requisition_order_status_is_default   BOOLEAN NOT NULL DEFAULT false,
    asset_requisition_order_status_is_completed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE asset_requisition_orders (
    asset_requisition_order_index        SERIAL PRIMARY KEY,
    employee_index                       INTEGER NOT NULL REFERENCES employees (employee_index) ON DELETE RESTRICT,
    department_index                     INTEGER REFERENCES departments (department_index) ON DELETE RESTRICT,
    asset_requisition_order_status_index INTEGER NOT NULL
        REFERENCES asset_requisition_order_statuses (asset_requisition_order_status_index) ON DELETE RESTRICT,
    admin_index                          INTEGER REFERENCES admins (admin_index) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_asset_requisition_orders_employee_index ON asset_requisition_orders (employee_index);
CREATE INDEX ix_asset_requisition_orders_department_index ON asset_requisition_orders (department_index);
CREATE INDEX ix_asset_requisition_orders_asset_requisition_order_status_index ON asset_requisition_orders (asset_requisition_order_status_index);
CREATE INDEX ix_asset_requisition_orders_admin_index ON asset_requisition_orders (admin_index);

CREATE TABLE asset_requisition_order_details (
    asset_requisition_order_detail_index SERIAL PRIMARY KEY,
    asset_requisition_order_index INTEGER NOT NULL
        REFERENCES asset_requisition_orders (asset_requisition_order_index) ON DELETE RESTRICT,
    asset_index                   INTEGER NOT NULL REFERENCES assets (asset_index) ON DELETE RESTRICT,
    asset_quantity                INTEGER NOT NULL,
    asset_price                   NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_asset_requisition_order_details_asset_requisition_order_index ON asset_requisition_order_details (asset_requisition_order_index);
CREATE INDEX ix_asset_requisition_order_details_asset_index ON asset_requisition_order_details (asset_index);

CREATE TABLE asset_requisition_order_comments (
    asset_requisition_order_comment_index SERIAL PRIMARY KEY,
    asset_requisition_order_index INTEGER NOT NULL
        REFERENCES asset_requisition_orders (asset_requisition_order_index) ON DELETE RESTRICT,
    employee_index                INTEGER NOT NULL REFERENCES employees (employee_index) ON DELETE RESTRICT,
    comment_detail                TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_asset_requisition_order_comments_asset_requisition_order_index ON asset_requisition_order_comments (asset_requisition_order_index);
CREATE INDEX ix_asset_requisition_order_comments_employee_index ON asset_requisition_order_comments (employee_index);

CREATE TABLE asset_holdings (
    asset_holding_index SERIAL PRIMARY KEY,
    asset_index         INTEGER NOT NULL REFERENCES assets (asset_index) ON DELETE RESTRICT,
    employee_index      INTEGER REFERENCES employees (employee_index) ON DELETE RESTRICT,
    department_index    INTEGER REFERENCES departments (department_index) ON DELETE RESTRICT,
    asset_requisition_order_detail_index INTEGER
        REFERENCES asset_requisition_order_details (asset_requisition_order_detail_index) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_asset_holdings_one_owner CHECK (num_nonnulls(employee_index, department_index) = 1)
);
CREATE INDEX ix_asset_holdings_asset_index ON asset_holdings (asset_index);
CREATE INDEX ix_asset_holdings_employee_index ON asset_holdings (employee_index);
CREATE INDEX ix_asset_holdings_department_index ON asset_holdings (department_index);
CREATE INDEX ix_asset_holdings_asset_requisition_order_detail_index ON asset_holdings (asset_requisition_order_detail_index);
