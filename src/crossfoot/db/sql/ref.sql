-- The `ref` schema: the two companion tables (spec 01 §3.5) and business-day arithmetic over
-- them. Idempotent, and deliberately free of extensions -- the CI Postgres service does not
-- mount docker/initdb/, so `vector` and `pg_trgm` are not available there.

CREATE SCHEMA IF NOT EXISTS ref;

CREATE TABLE IF NOT EXISTS ref.fx_rates (
    rate_date      date   NOT NULL,
    quote_currency text   NOT NULL,
    rate_ppm       bigint NOT NULL,
    rate           numeric(12,6) GENERATED ALWAYS AS (rate_ppm::numeric / 1000000) STORED,
    PRIMARY KEY (rate_date, quote_currency)
);

COMMENT ON TABLE ref.fx_rates IS
    'GBP base; the rate is foreign units per GBP. Weekends and holidays carry the previous '
    'business day''s rate, as the generator emitted them.';
COMMENT ON COLUMN ref.fx_rates.rate_ppm IS
    'Source of truth: integer parts per million. The generator carries rates as integers so '
    'that byte-identity survives libm differences across platforms.';
COMMENT ON COLUMN ref.fx_rates.rate IS
    'Derived for SQL convenience. numeric, so it is exact decimal -- never float.';

CREATE TABLE IF NOT EXISTS ref.bank_holidays (
    holiday_date date PRIMARY KEY,
    name         text NOT NULL,
    region       text NOT NULL DEFAULT 'GB-ENG'
);

COMMENT ON TABLE ref.bank_holidays IS
    'England & Wales bank holidays from holidays_gb_eng.csv. The only holiday source in the '
    'system: the generator and the matcher both read this list and nothing else (spec 01 §3.5).';

-- Mirrors crossfoot.generate.calendars.is_business_day: Mon-Fri and not a bank holiday.
CREATE OR REPLACE FUNCTION ref.is_business_day(day date) RETURNS boolean
    LANGUAGE sql STABLE
AS $$
    SELECT extract(isodow FROM day) < 6
       AND NOT EXISTS (SELECT 1 FROM ref.bank_holidays h WHERE h.holiday_date = day);
$$;

-- Mirrors crossfoot.generate.calendars.add_business_days, including the n = 0 case, which
-- snaps *forward* to a business day rather than moving.
CREATE OR REPLACE FUNCTION ref.add_business_days(day date, n integer) RETURNS date
    LANGUAGE plpgsql STABLE
AS $$
DECLARE
    result date := day;
    step   integer := CASE WHEN n < 0 THEN -1 ELSE 1 END;
BEGIN
    IF n = 0 THEN
        WHILE NOT ref.is_business_day(result) LOOP
            result := result + 1;
        END LOOP;
        RETURN result;
    END IF;
    FOR _step IN 1..abs(n) LOOP
        result := result + step;
        WHILE NOT ref.is_business_day(result) LOOP
            result := result + step;
        END LOOP;
    END LOOP;
    RETURN result;
END;
$$;
