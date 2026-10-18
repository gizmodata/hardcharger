# Makes DuckDB's Arrow export refuse the HUGEINT and UHUGEINT values that Arrow's
# decimal128(38, 0) can't hold, instead of sending a wrong number.
#
# Without arrow_lossless_conversion, DuckDB declares both types as decimal128(38, 0) and
# copies the 16 bytes as they are. A decimal128(38, 0) holds 38 digits, so a value of 10^38
# or more (in magnitude) is not a valid one, and UHUGEINT's upper half (2^127 and up) arrives
# as a negative number: UHUGEINT's maximum reads as -1. With the patch, such a value fails the
# statement with a conversion error that names the value and the ways around it; every
# value that fits is sent exactly as before. With arrow_lossless_conversion on, both types
# keep DuckDB's own 16-byte extension types, which hold every value.
#
# A source that no longer matches is a build error, never a silent skip.
#
# Run as: cmake -DDUCKDB_SOURCE_DIR=<dir> -P patch-duckdb-arrow-hugeint.cmake

set(APPENDER_FILE "${DUCKDB_SOURCE_DIR}/src/common/arrow/arrow_appender.cpp")
set(MARKER "HardchargerDecimal38Converter")

file(READ "${APPENDER_FILE}" CONTENT)
string(FIND "${CONTENT}" "${MARKER}" already)
if(NOT already EQUAL -1)
  message(STATUS "patch-duckdb-arrow-hugeint: ${APPENDER_FILE} is already patched")
  return()
endif()

# Each anchor must occur exactly once.
function(require_once text what)
  string(FIND "${CONTENT}" "${text}" first_at)
  string(FIND "${CONTENT}" "${text}" last_at REVERSE)
  if(first_at EQUAL -1 OR NOT first_at EQUAL last_at)
    message(FATAL_ERROR "patch-duckdb-arrow-hugeint: ${what} in ${APPENDER_FILE} was not found "
                        "exactly once; update server/scripts/patch-duckdb-arrow-hugeint.cmake for this DuckDB")
  endif()
endfunction()

# 1. The converter, at the top of the duckdb namespace. (string(CONCAT) of quoted pieces:
# the C++ contains semicolons, which an unquoted list expansion would split on.)
set(NAMESPACE_ANCHOR "#include \"duckdb/main/config.hpp\"\n\nnamespace duckdb {\n")
require_once("${NAMESPACE_ANCHOR}" "the start of the duckdb namespace")
string(CONCAT CONVERTER
  "#include \"duckdb/main/config.hpp\"\n"
  "#include \"duckdb/common/types/hugeint.hpp\"\n"
  "#include \"duckdb/common/types/uhugeint.hpp\"\n"
  "#include \"duckdb/common/exception/conversion_exception.hpp\"\n\n"
  "namespace duckdb {\n\n"
  "// Hardcharger: HUGEINT and UHUGEINT leave as decimal128(38, 0), which holds 38 digits; a\n"
  "// value with more is refused rather than sent wrong (UHUGEINT's upper half would arrive\n"
  "// negative). server/scripts/patch-duckdb-arrow-hugeint.cmake.\n"
  "struct HardchargerDecimal38Converter {\n"
  "\t[[noreturn]] static void Refuse(const string &type, const string &value) {\n"
  "\t\tthrow ConversionException(\"Hardcharger cannot send the \" + type + \" value \" + value +\n"
  "\t\t                          \" as Arrow decimal128(38, 0), which holds at most 38 digits. CAST it to \"\n"
  "\t\t                          \"VARCHAR, or run SET SESSION arrow_lossless_conversion = true \"\n"
  "\t\t                          \"(DuckDB's 16-byte \" + type + \" Arrow type, which holds every value)\");\n"
  "\t}\n"
  "\tstatic hugeint_t Check(hugeint_t input) {\n"
  "\t\tconst hugeint_t &limit = Hugeint::POWERS_OF_TEN[38];\n"
  "\t\tif (input >= limit || input <= -limit) {\n"
  "\t\t\tRefuse(\"HUGEINT\", Hugeint::ToString(input));\n"
  "\t\t}\n"
  "\t\treturn input;\n"
  "\t}\n"
  "\tstatic hugeint_t Check(uhugeint_t input) {\n"
  "\t\tif (input >= Uhugeint::POWERS_OF_TEN[38]) {\n"
  "\t\t\tRefuse(\"UHUGEINT\", Uhugeint::ToString(input));\n"
  "\t\t}\n"
  "\t\thugeint_t result;\n"
  "\t\tresult.upper = static_cast<int64_t>(input.upper);\n"
  "\t\tresult.lower = input.lower;\n"
  "\t\treturn result;\n"
  "\t}\n"
  "\ttemplate <class TGT, class SRC>\n"
  "\tstatic TGT Operation(SRC input) {\n"
  "\t\treturn Check(input);\n"
  "\t}\n"
  "\t// A NULL's bytes are whatever the vector held: never checked, sent as zero.\n"
  "\tstatic bool SkipNulls() {\n"
  "\t\treturn true;\n"
  "\t}\n"
  "\ttemplate <class TGT>\n"
  "\tstatic void SetNull(TGT &value) {\n"
  "\t\tvalue = TGT();\n"
  "\t}\n"
  "};\n")
string(REPLACE "${NAMESPACE_ANCHOR}" "${CONVERTER}" CONTENT "${CONTENT}")

# 2. The two appenders: checked, unless the session asked for the lossless types.
string(CONCAT APPENDERS_ANCHOR
  "\tcase LogicalTypeId::HUGEINT:\n"
  "\t\tInitializeAppenderForType<ArrowScalarData<hugeint_t>>(append_data);\n"
  "\t\tbreak;\n"
  "\tcase LogicalTypeId::UHUGEINT:\n"
  "\t\tInitializeAppenderForType<ArrowScalarData<uhugeint_t>>(append_data);\n"
  "\t\tbreak;\n")
require_once("${APPENDERS_ANCHOR}" "the HUGEINT and UHUGEINT appenders")
string(CONCAT APPENDERS
  "\tcase LogicalTypeId::HUGEINT:\n"
  "\t\tif (append_data.options.arrow_lossless_conversion) {\n"
  "\t\t\tInitializeAppenderForType<ArrowScalarData<hugeint_t>>(append_data);\n"
  "\t\t} else {\n"
  "\t\t\tInitializeAppenderForType<ArrowScalarData<hugeint_t, hugeint_t, HardchargerDecimal38Converter>>(append_data);\n"
  "\t\t}\n"
  "\t\tbreak;\n"
  "\tcase LogicalTypeId::UHUGEINT:\n"
  "\t\tif (append_data.options.arrow_lossless_conversion) {\n"
  "\t\t\tInitializeAppenderForType<ArrowScalarData<uhugeint_t>>(append_data);\n"
  "\t\t} else {\n"
  "\t\t\tInitializeAppenderForType<ArrowScalarData<hugeint_t, uhugeint_t, HardchargerDecimal38Converter>>(append_data);\n"
  "\t\t}\n"
  "\t\tbreak;\n")
string(REPLACE "${APPENDERS_ANCHOR}" "${APPENDERS}" CONTENT "${CONTENT}")

file(WRITE "${APPENDER_FILE}" "${CONTENT}")
message(STATUS "patch-duckdb-arrow-hugeint: HUGEINT and UHUGEINT Arrow export checks added to ${APPENDER_FILE}")
