import os

import ibis
import snowflake.connector
from plotnine import (
    aes,
    element_blank,
    geom_col,
    geom_histogram,
    ggplot,
    labs,
    theme,
    theme_minimal,
)
from posit.connect.external.snowflake import PositAuthenticator
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

WAREHOUSE = "DEFAULT_WH"
DATABASE = "SNOWFLAKE_PUBLIC_DATA_FREE"
SCHEMA = "PUBLIC_DATA_FREE"
TABLE = "HOME_MORTGAGE_DISCLOSURE_ATTRIBUTES"

LOAN_TYPES = ["All", "Conventional", "FHA", "VA", "FSA/RHS"]
STATES = ["All", "California", "Texas", "Florida", "New York", "Pennsylvania"]


def credentials(session):
    """Snowflake connection parameters for the current viewer.

    On Connect, the viewer's session token is exchanged for a Snowflake OAuth
    token, so every query runs as the person looking at the dashboard. In
    Workbench, the managed credentials in connections.toml are used instead.
    """
    # POSIT_PRODUCT since Connect 2025.02.0; RSTUDIO_PRODUCT is the deprecated
    # spelling, kept as a fallback for older Connect versions.
    on_connect = "CONNECT" in (
        os.getenv("POSIT_PRODUCT"),
        os.getenv("RSTUDIO_PRODUCT"),
    )
    if on_connect:
        token = session.http_conn.headers.get("Posit-Connect-User-Session-Token")
        if token is None:
            raise RuntimeError(
                "No Posit-Connect-User-Session-Token header. Attach the "
                "Snowflake integration to this content in Connect."
            )
        # Connect sets SNOWFLAKE_ACCOUNT from the attached integration.
        account = os.getenv("SNOWFLAKE_ACCOUNT")

        auth = PositAuthenticator(
            local_authenticator="EXTERNALBROWSER",
            user_session_token=token,
        )
        return {
            "account": account,
            "authenticator": auth.authenticator,
            "token": auth.token,
        }

    if os.getenv("SNOWFLAKE_HOME") is not None:
        return {"connection_name": "workbench"}

    raise RuntimeError(
        "No Snowflake credentials found. Run this app in Posit Workbench or "
        "deploy it to Posit Connect with the Snowflake integration attached."
    )


def get_table(creds):
    """A lazy Ibis reference to the HMDA table, for one viewer."""
    con = snowflake.connector.connect(
        warehouse=WAREHOUSE,
        database=DATABASE,
        schema=SCHEMA,
        **creds,
    )
    ibiscon = ibis.snowflake.from_connection(con, create_object_udfs=False)
    return con, ibiscon.table(TABLE)


app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_select("loan_type", "Loan type", choices=LOAN_TYPES),
        ui.input_select("state", "State", choices=STATES),
        ui.input_slider(
            "loan_amount",
            "Loan amount range",
            min=0,
            max=1_000_000,
            value=(0, 500_000),
            step=50_000,
            pre="$",
        ),
    ),
    ui.layout_columns(
        ui.value_box("Total loans", ui.output_text("total_loans")),
        ui.value_box("Average loan amount", ui.output_text("avg_loan")),
        ui.value_box("Total loan volume", ui.output_text("total_volume")),
        fill=False,
    ),
    ui.layout_columns(
        ui.card(
            ui.card_header("Distribution of loan amounts"),
            ui.output_plot("loan_plot"),
        ),
        ui.card(
            ui.card_header("Loan count by year"),
            ui.output_plot("year_plot"),
        ),
    ),
    title="HMDA Mortgage Data Explorer",
    fillable=True,
)


def server(input: Inputs, output: Outputs, session: Session):
    # One connection per session, built from this viewer's credentials.
    # Everything downstream inherits their Snowflake grants.
    creds = credentials(session)
    con, mortgage_data = get_table(creds)
    session.on_ended(con.close)

    @reactive.calc
    def filtered():
        """The filters, as a lazy Ibis expression. No rows move yet."""
        low, high = input.loan_amount()
        data = mortgage_data.filter(
            mortgage_data.LOAN_AMOUNT >= low,
            mortgage_data.LOAN_AMOUNT <= high,
        )
        if input.loan_type() != "All":
            data = data.filter(mortgage_data.LOAN_TYPE == input.loan_type())
        if input.state() != "All":
            data = data.filter(mortgage_data.STATE_NAME == input.state())
        return data

    @reactive.calc
    def metrics():
        """Three scalars, computed by Snowflake rather than in Python."""
        return (
            filtered()
            .aggregate(
                n=lambda t: t.count(),
                avg=lambda t: t.LOAN_AMOUNT.mean(),
                total=lambda t: t.LOAN_AMOUNT.sum(),
            )
            .to_pandas()
            .iloc[0]
        )

    @reactive.calc
    def by_year():
        """One row per year, aggregated in the database."""
        return (
            filtered()
            .group_by("YEAR")
            .aggregate(count=lambda t: t.count())
            .order_by("YEAR")
            .to_pandas()
        )

    @reactive.calc
    def amount_sample():
        """A bounded batch of loan amounts, for the histogram.

        The filtered table can be hundreds of millions of rows, so cap what
        comes back rather than collecting everything. Note this is the first
        50,000 rows Snowflake produces, not a random sample, so the histogram
        reflects those rows rather than the whole filtered set.
        """
        return filtered().select("LOAN_AMOUNT").limit(50_000).to_pandas()

    @render.text
    def total_loans():
        return f"{int(metrics()['n']):,}"

    @render.text
    def avg_loan():
        avg = metrics()["avg"]
        return "n/a" if avg is None else f"${avg:,.0f}"

    @render.text
    def total_volume():
        total = metrics()["total"]
        return "n/a" if total is None else f"${total:,.0f}"

    @render.plot
    def loan_plot():
        df = amount_sample()
        return (
            ggplot(df, aes(x="LOAN_AMOUNT"))
            + geom_histogram(bins=30, fill="#4A90D9", color="white")
            + labs(x="Loan amount", y="Applications")
            + theme_minimal()
        )

    @render.plot
    def year_plot():
        df = by_year()
        df["YEAR"] = df["YEAR"].astype(str)
        return (
            ggplot(df, aes(x="YEAR", y="count"))
            + geom_col(fill="#4A90D9")
            + labs(x="Year", y="Applications")
            + theme_minimal()
            + theme(panel_grid_minor=element_blank())
        )


app = App(app_ui, server)
