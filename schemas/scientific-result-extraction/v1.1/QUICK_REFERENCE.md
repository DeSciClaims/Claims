# Claims extraction schema 1.1: quick reference

Status: **Draft — under development.**

Source: [Claims Extraction Schema 1.1](./schema.json). This guide summarizes its fields, organization rules, and vocabularies. The JSON defines exact requirements. `[]` means a list of entries.

Worked examples: four complete extractions with their table CSVs are in [`examples/`](./examples/README.md).

## 1. Data collected

The extraction records scientific values, equations, their interpretation, source evidence, and complete numerical tables.

### Scientific objects: `fields[]`

Every object requires **`id`, `field`, `label`, and `value`**. Other properties supply meaning, provenance, or links as needed.

| Field | Meaning |
|---|---|
| `id` | Permanent object identifier. |
| `field` | Descriptive name, such as `estimate` or `cf_mean`. |
| `label` | Controlled object category. |
| `value` | Number, text, equation, interval, or structured collection. |
| `field_group` | Optional vocabulary guide. |
| `units` | Measurement units or display scale. |
| `value_operator` | Equality, bound, or approximation. |
| `measure` | Numerical form of an estimate. |
| `confidence_level` | Confidence probability; e.g., `0.95`. |
| `description` | Explanation of the object. |
| `context` | Information needed to interpret it. |
| `references[]` | Named links to existing objects. |
| `parent_field_id` | Containing scientific collection's F ID. |
| `field_path` | Current nested location; relative to parent when given. |
| `origin` | How the value was obtained. |
| `value_status` | Precision or source-consistency qualification. |
| `source_snippets[]` | Verbatim evidence and source locations. |
| `table_references[]` | Source table cells. |
| `notes` | Additional qualifications. |

Preserve signs, units, bounds, and reported precision. `null` means no supplied value, never zero. `parent_field_id` requires `field_path`; array indexes in that path start at zero.

### Interpretive context

`context` accepts text or an open object. These names are suggestions, not required fields or fixed categories.

| Name | Meaning |
|---|---|
| `outcome` | What is measured or modeled. |
| `treatment` | Evaluated intervention. |
| `comparator` | Reference condition. |
| `exposure` | Studied exposure or predictor. |
| `population` | Subjects or units studied. |
| `sample` | Relevant analysis subset. |
| `time` | Time point/window and reference event. |
| `method` | Measurement, calculation, or test. |
| `model` | Statistical or theoretical model. |
| `adjustment` | Covariates or other adjustments. |
| `weighting` | Weighting procedure. |
| `assumptions` | Conditions for validity. |
| `direction` | Contrast orientation and sign meaning. |
| `counted_entity` | What is counted; relevant denominator. |

### Source and extraction information

Available fields, with explanations only where the names need them:

- **`paper`**: `id` (paper key), `title`, `authors`, `year`, `journal`, `doi`, `url`, `citation` (other bibliographic details), `notes`.
- **`extraction`**: `id` (extraction key), `revision`, `date`, `scope` (sources/content covered), `table_coverage`, `notes`.
- **`sources[]`**: `id` (document key), `title`, `kind` (document type), `doi`, `url`, `version`, `notes`.
- **`source_snippets[]`**: `source_id` (document), `locator` (location), `text` (verbatim excerpt), `note` (transcription qualification).
- **`references[]`**: `field` (local role), `target_id` (existing object), `note` (explanation).
- **`extraction.table_coverage`**: `status` (export completeness), `reviewed_source_ids` (documents inspected), `notes` (gaps or absence of tables).

### Numerical tables: `tables[]`

| Field | Meaning |
|---|---|
| `id` | Permanent T identifier. |
| `source_id` | Containing document. |
| `locator` | Table number and source location. |
| `title` | Table name. |
| `caption` | Complete source caption. |
| `csv_file` | Export filename: table ID plus `.csv`. |
| `columns[]` | Exported columns, in CSV order. |
| `row_count` | Data records, excluding the header. |
| `annotations[]` | Footnotes, units, and layout qualifications. |
| `notes` | Discrepancies or transcription decisions. |

Nested table fields:

- **`columns[]`**: `name` (unique CSV key), `header_path` (original heading hierarchy), `role` (column purpose), `units`, `description`.
- **`annotations[]`**: `kind` (annotation type), `text`, `marker` (source symbol), `rows` and `columns` (affected cells), `source_locator`. Omitted scope means all rows/columns; merged cells require both lists.
- **`fields[].table_references[]`**: `table_id`, `row` (CSV data record, starting at 1), `column` (CSV key), `component` (part of a composite cell), `note`.

### Data and code availability: `data_code_availability` (optional)

Records what the paper says about access to its data and code, and every dataset or code repository it names with a link or identifier. Links are copied as printed; they are never fetched, opened, or run during extraction.

| Field | Meaning |
|---|---|
| `data`, `code` | Separate availability statements, each with `status`, `source_snippets[]`, `notes`. |
| `artifacts[]` | One entry per distinct dataset, code repository, or other artifact named with a URL or identifier. Empty list when none. |
| `reviewed_source_ids` | Documents inspected for statements and links. |
| `notes` | Coverage gaps. |

Nested fields:

- **`artifacts[]`**: `id` (permanent A identifier), `artifact_type`, `url` and/or `identifier` (as printed; at least one), `identifier_type` (e.g. doi, accession), `repository` (e.g. GitHub, Zenodo, GEO), `description`, `provenance`, `access`, `source_snippets[]` (must contain the URL or identifier verbatim), `related_ids` (results, groups, context, or tables that use it; never closure inputs), `notes`.
- A statement marked `available`, `available_on_request`, `restricted`, or `not_available` requires a verbatim snippet.

### Relationships and organization records

**`relationships[]`** records each connection:

| Field | Meaning |
|---|---|
| `source_id` | Starting object. |
| `relation` | Controlled relationship category. |
| `target_id` | Target object. |
| `role` | Comparison or table-use role. |
| `closure_direction` | Whether/how the link builds local results. |
| `context` | Scope of the connection. |
| `basis` | Source or reasoning establishing it. |
| `note` | Explanation; required for `other`. |
| `source_snippets[]` | Supporting source evidence. |

The following records all allow `id`, `title`, `summary` (paraphrase), `context`, `references[]`, `source_snippets[]`, and `notes`. Their additional fields are:

| Record | Additional fields |
|---|---|
| `results[]` | `fields[]`: stored objects; `group_id`: primary family; `closure`: computed local membership. |
| `result_groups[]` | `question`: substantive question; `investigation`: generating study/mechanism; `grouping_reason`: family rationale; `core_result_ids`: main results; `related_group_ids`: related families. |
| `shared_context[]` | `fields[]`: reusable sample, parameter, or context objects. |
| `cross_group_summaries[]` | `fields[]`: summary objects; `group_ids`: families summarized. |

Each result's **`closure`** contains:

| Field | Meaning |
|---|---|
| `core_ids` | Local result's terminal object(s) or comparison anchor. |
| `member_ids` | Core plus all objects feeding it through local links. |
| `reference_ids` | Related objects outside that membership. |
| `status` | Resolved or requiring review. |
| `notes` | Boundary, cycle, or unresolved-issue explanation. |

**`closure_review[]`** uses `object_id` (item needing attention), `reason` (issue), and `disposition` (how handled).

**Required package sections:** `schema_version` = `"1.1"`, `paper`, `extraction`, `sources`, `tables`, `results`, `result_groups`. Optional sections: `data_code_availability`, `shared_context`, `relationships`, `cross_group_summaries`, `closure_review`, and document-level `notes`. Relationships become necessary where grouping or table-link rules require them.

## 2. Labeling and organization rules

### Assign labels and preserve identity

1. **Label the object's meaning.** A descriptive `field` name remains free-form; `label` uses a controlled code. Use the same label wherever that kind of object appears.
2. **Use `other` when needed.** Preserve the meaning in `value`, `description`, or `context`.
3. **Separate role from numerical form.** For example: `label: association`, `measure: odds_ratio`. Preserve the study's interpretation.
4. **Keep statistical distinctions.** SE describes estimate uncertainty; SD describes variation. A whole CI differs from an endpoint. Sample size differs from other counts. Attach each uncertainty measure or p-value to its actual estimate or test.
5. **Define shared objects once.** Reuse IDs for clearly identical or repeated entries. Equal numbers alone do not establish identity. Preserve IDs when correcting labels or moving objects.

ID keys: **E** extraction; **R** result; **C** shared context; **F** scientific field; **G** family; **S** source; **T** table; **A** data or code artifact. Example field ID: `P001.E001.R002.F003`. ID allocation does not determine group membership.

### Build local results, then result families

1. **Identify objects and review their connections.** Record each grouping link explicitly with `closure_direction`. Missing direction means unreviewed; `none` means deliberately non-closing.
2. **Choose a meaningful local core.** An estimate, comparison, descriptive result, or mathematical output can qualify. An unsupported graph endpoint does not automatically become a result.
3. **Collect its local inputs.** A local result contains the core and every object that can reach it through reviewed subordinate-to-core links. `parent_field_id` also adds a child-to-parent link. Storage arrays and family membership do not determine this set.
4. **Keep boundary references.** Retain external context and non-closing connections without importing their contents. Shared objects may belong to several closures without merging them. Unresolved links or cycles require explanation and `review_required`.
5. **Form families afterward.** Combine local results answering one substantive question within the same identifiable investigation, review, or mechanism. Give each ordinary result one `group_id`; explain the boundary and choose its family's main `core_result_ids`.

| Family decision | Rule |
|---|---|
| Keep together | Outcome, time, dose, subgroup, adjustment, or parameter variations within the same question and investigation. |
| Split | Distinct question, independently identified investigation, review, or generating mechanism. |
| Insufficient to merge | Shared data, inputs, equations, table, or broad topic alone. |
| Still ambiguous | Within a supported common investigation, keep together and record the alternative boundary and reason. |

`closure.core_ids` identifies a local graph core; `core_result_ids` identifies a family's main result records. A cross-group summary references existing families without creating additional independent evidence. `closed` describes the represented local graph, not complete paper coverage.

### Handle tables independently

- Export **every numerical table**, including descriptive tables and all associated supplements. Text labels are allowed; numerical expressions count as numbers. Mixed tables with a numerical region are exported in full.
- Use **one T object and one complete CSV per source table**, including its panels and page continuations. Preserve source values, missing markers, row identities, headings, units, and footnotes. Keep composite cells intact; a merged numerical cell is transcribed once with its span annotated.
- Link each table to the relevant ordinary results using `table_for`. Every T link has `closure_direction: none`; tables never become local cores or members. Include linked tables in boundary references, even when the linked R record is not itself a member.
- Give independently extracted cell values `table_references`. For values in headers, captions, or footnotes, use a source snippet and named table reference. Missing required supplements or incomplete exports require `table_coverage.status: incomplete` and an explanation.

## 3. Controlled vocabularies

Use codes exactly as written. The lists below are closed choices, except that **field-group mappings are advisory** and the final subsection explicitly lists open fields.

### Object labels: `label`

| Code | Meaning |
|---|---|
| `mean` | Reported mean. |
| `count` | Number of specified items or events. |
| `proportion` | Share of a specified set. |
| `probability` | Probability of a specified event. |
| `sample_size` | Units in the relevant sample/analysis. |
| `treatment_effect` | Intervention–reference outcome contrast. |
| `association` | Exposure/predictor–outcome relationship. |
| `standard_error` | Standard error of an estimate. |
| `standard_deviation` | Standard deviation of observations/values. |
| `confidence_interval` | Whole confidence interval. |
| `confidence_limit` | Lower or upper confidence endpoint. |
| `p_value` | P-value of an identified test. |
| `outcome` | Measured or modeled outcome. |
| `intervention` | Treatment, action, or experimental condition. |
| `exposure` | Studied exposure or predictor. |
| `group` | Sample or population group. |
| `time_point` | Single measurement/event time. |
| `time_interval` | Duration or time window. |
| `parameter` | Model parameter or numerical setting. |
| `threshold` | Classification or condition cutoff. |
| `increment` | Input change defining an effect's scale. |
| `equation` | Equation, formula, or inequality. |
| `assumption` | Premise underlying a result. |
| `condition` | Requirement attached to a result. |
| `absolute_change` | Difference in outcome units. |
| `relative_change` | Change relative to a reference value. |
| `odds_ratio` | Ratio of odds. |
| `other` | Unlisted or uncertain category. |

**`measure` choices:** `absolute_change`, `relative_change`, `odds_ratio`, `proportion`, `other`. Meanings are as above.

### Field groups: `field_group`

These optional guides suggest labels; they do not restrict a field's label or require separate containers.

| Code | Typical labels |
|---|---|
| `result_estimate` | `treatment_effect`, `association`, `proportion`, `probability`, `other` |
| `descriptive_value` | `mean`, `count`, `proportion`, `other` |
| `effect_measure` | `absolute_change`, `relative_change`, `odds_ratio`, `proportion`, `other` |
| `uncertainty_variation` | `standard_error`, `standard_deviation`, `confidence_interval`, `confidence_limit`, `other` |
| `test_result` | `p_value`, `other` |
| `sample_evidence_count` | `sample_size`, `count`, `other` |
| `outcome_field` | `outcome`, `other` |
| `intervention_exposure_comparator` | `intervention`, `exposure`, `group`, `other` |
| `time` | `time_point`, `time_interval`, `other` |
| `numerical_context` | `parameter`, `threshold`, `increment`, `count`, `other` |
| `mathematics` | `equation`, `parameter`, `assumption`, `condition`, `other` |
| `other` | No listed field group fits. |

### Relationship labels: `relation`

Arrows show scientific source-to-target direction, independently of `closure_direction`.

| Code | Meaning and direction |
|---|---|
| `uncertainty_of` | SE/CI/limit → estimate. |
| `spread_of` | SD → described group, distribution, or value. |
| `p_value_of` | P-value → its actual test, comparison, or tested estimate. |
| `describes` | Value/count → described sample, group, outcome, or result. |
| `compares` | Comparison → evaluated or reference input. |
| `derived_from` | Output → input or preceding result. |
| `contributes_to` | Contributing result → pooled result. |
| `applies_under` | Result/equation → setting, assumption, or condition. |
| `table_for` | Source table → ordinary result. |
| `other` | Explained relationship; `note` required. |

### Directions, roles, and statuses

| Field | Allowed values and meanings |
|---|---|
| `closure_direction` | `source_to_target`: source feeds target; `target_to_source`: target feeds source; `none`: no local grouping edge. |
| `role` for `compares` | `evaluated`: evaluated input; `reference`: comparison baseline. Required. |
| `role` for `table_for` | `reports_result`: reports the result; `describes_sample`: describes its sample; `provides_inputs`: supplies inputs; `provides_context`: supplies context. Required. |
| `closure.status` | `closed`: local boundary resolved; `review_required`: unresolved, with `notes`. |
| `table_coverage.status` | `complete`: all required tables exported; `incomplete`: coverage/export gaps, with `notes`. |
| `columns[].role` | `data`: source values; `label`: row identity; `context`: inherited/panel labels. |
| `annotations[].kind` | `footnote`, `unit`, `statistical_convention`, `panel`, `merged_cell`, `missing_value`, `structural_blank`, `transcription`, `other`. |
| `data.status`, `code.status` | `available`: publicly accessible or location named; `available_on_request`: via authors or a committee; `restricted`: application, licence, controlled access, or embargo; `not_available`: stated as not shareable; `not_stated`: no statement in the reviewed sources; `not_applicable`: no such material (e.g. no code for a purely analytical paper). |
| `artifacts[].artifact_type` | `dataset`, `code`, `other` (explain in `description`). |
| `artifacts[].provenance` | `generated_by_this_paper`, `reused` (third-party or previously published), `unclear`. |
| `artifacts[].access` | `open`, `on_request`, `restricted`, `unclear`; as stated, not tested. |

`structural_blank` means an empty cell introduced by flattening the layout; it does not mean source-reported missing data. A `table_for` link also requires `closure_direction: none`.

### Open fields and value formats

- **Open text/names:** `field`, `units`, `description`, `notes`, `origin`, `value_status`, `value_operator`, source `kind`, artifact `identifier_type` and `repository`, and context properties. Use clear source-specific wording.
- **Illustrative operators:** `=`, `<`, `<=`, `>`, `>=`, `approximately`, `nearly`; these are examples, not an exhaustive list.
- **Illustrative origins:** `source_reported`, `source_calculated`, `extractor_calculated`, `digitized`; these are examples, not an exhaustive list.
- **`value` types:** number, string, boolean, array, object, or `null`.
- **`confidence_level`:** probability greater than 0 and no greater than 1.
