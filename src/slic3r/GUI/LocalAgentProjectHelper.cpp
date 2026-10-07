#include "LocalAgentProjectHelper.hpp"

#include "GUI_App.hpp"
#include "GUI_Init.hpp"
#include "MainFrame.hpp"
#include "Plater.hpp"
#include "libslic3r/Config.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"

#include <openssl/sha.h>
#include <nlohmann/json.hpp>
#include <wx/app.h>
#include <wx/utils.h>

#include <array>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <map>
#include <set>
#include <stdexcept>
#include <sstream>
#include <string>
#include <vector>
#include <boost/log/trivial.hpp>
#include <boost/filesystem.hpp>

namespace Slic3r { namespace GUI {
namespace {
using json = nlohmann::json;
namespace fs = std::filesystem;

std::string sha256_file(const fs::path &path)
{
    std::ifstream in(path, std::ios::binary);
    if (!in)
        throw std::runtime_error("A required local file could not be read.");
    SHA256_CTX ctx;
    SHA256_Init(&ctx);
    std::array<char, 64 * 1024> buffer{};
    while (in) {
        in.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto count = in.gcount();
        if (count > 0)
            SHA256_Update(&ctx, buffer.data(), static_cast<size_t>(count));
    }
    if (!in.eof())
        throw std::runtime_error("A required local file could not be read.");
    unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256_Final(digest, &ctx);
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (unsigned char byte : digest)
        out << std::setw(2) << static_cast<unsigned int>(byte);
    return out.str();
}

std::string sha256_text(const std::string &value)
{
    unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256(reinterpret_cast<const unsigned char *>(value.data()), value.size(), digest);
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (unsigned char byte : digest)
        out << std::setw(2) << static_cast<unsigned int>(byte);
    return out.str();
}

json parse_json_no_duplicate_keys(const std::string &text)
{
    std::vector<std::set<std::string>> object_keys;
    auto callback = [&object_keys](int, json::parse_event_t event, json &parsed) {
        if (event == json::parse_event_t::object_start) {
            object_keys.emplace_back();
        } else if (event == json::parse_event_t::object_end) {
            if (!object_keys.empty())
                object_keys.pop_back();
        }
        if (event == json::parse_event_t::key) {
            const std::string key = parsed.get<std::string>();
            if (object_keys.empty() || !object_keys.back().insert(key).second)
                throw std::runtime_error("JSON contains a duplicate key.");
        }
        return true;
    };
    return json::parse(text, callback);
}

void reject_nonfinite_json_numbers(const json &value)
{
    if (value.is_number_float() && !std::isfinite(value.get<double>()))
        throw std::runtime_error("JSON contains a non-finite number.");
    if (value.is_array()) {
        for (const auto &item : value)
            reject_nonfinite_json_numbers(item);
    } else if (value.is_object()) {
        for (const auto &item : value.items())
            reject_nonfinite_json_numbers(item.value());
    }
}

bool has_nonfinite_numeric_token(const std::string &value)
{
    std::string token;
    auto inspect = [&token]() {
        if (token.empty())
            return false;
        char *end = nullptr;
        const double parsed = std::strtod(token.c_str(), &end);
        const bool numeric_token = end != token.c_str() && end != nullptr && *end == '\0';
        token.clear();
        return numeric_token && !std::isfinite(parsed);
    };
    for (unsigned char ch : value) {
        if (std::isalnum(ch) || ch == '.' || ch == '+' || ch == '-')
            token.push_back(static_cast<char>(ch));
        else if (inspect())
            return true;
    }
    return inspect();
}

void reject_nonfinite_config_option(const ConfigOption &option)
{
    const int raw_type = static_cast<int>(option.type());
    const int scalar_type = (raw_type & static_cast<int>(coVectorType)) ?
        raw_type - static_cast<int>(coVectorType) : raw_type;
    if (scalar_type != coFloat && scalar_type != coInt && scalar_type != coPercent &&
        scalar_type != coFloatOrPercent && scalar_type != coPoint && scalar_type != coPoint3)
        return;
    if (const auto *vector_option = dynamic_cast<const ConfigOptionVectorBase *>(&option)) {
        for (const std::string &item : vector_option->vserialize())
            if (has_nonfinite_numeric_token(item))
                throw std::runtime_error("A profile contains a non-finite numeric value.");
    } else if (has_nonfinite_numeric_token(option.serialize())) {
        throw std::runtime_error("A profile contains a non-finite numeric value.");
    }
}

bool contains_nonfinite_config_token(const json &value)
{
    if (value.is_string())
        return has_nonfinite_numeric_token(value.get<std::string>());
    if (value.is_array()) {
        for (const json &item : value)
            if (contains_nonfinite_config_token(item))
                return true;
    }
    return false;
}

bool numeric_config_option_type(ConfigOptionType type)
{
    const int raw_type = static_cast<int>(type);
    const int scalar_type = (raw_type & static_cast<int>(coVectorType)) ?
        raw_type - static_cast<int>(coVectorType) : raw_type;
    return scalar_type == coFloat || scalar_type == coInt || scalar_type == coPercent ||
           scalar_type == coFloatOrPercent || scalar_type == coPoint || scalar_type == coPoint3;
}

json read_strict_json_file(const fs::path &path, uintmax_t maximum_size)
{
    std::error_code ec;
    const uintmax_t size = fs::file_size(path, ec);
    if (ec || size > maximum_size)
        throw std::runtime_error("A normalization input is missing or exceeds the supported size.");
    std::ifstream in(path, std::ios::binary);
    if (!in)
        throw std::runtime_error("A normalization input could not be read.");
    std::string text(static_cast<size_t>(size), '\0');
    if (size > 0)
        in.read(text.data(), static_cast<std::streamsize>(size));
    if (in.bad() || static_cast<uintmax_t>(in.gcount()) != size)
        throw std::runtime_error("A normalization input could not be read completely.");
    char extra_byte = 0;
    if (in.get(extra_byte))
        throw std::runtime_error("A normalization input changed while it was being read.");
    if (in.bad())
        throw std::runtime_error("A normalization input could not be read completely.");
    json parsed = parse_json_no_duplicate_keys(text);
    reject_nonfinite_json_numbers(parsed);
    return parsed;
}

void write_atomic_new(const fs::path &path, const json &result)
{
    if (fs::exists(path))
        throw std::runtime_error("The normalization output already exists.");
    static std::atomic<unsigned long long> serial{0};
    fs::path temporary_directory;
    for (int attempt = 0; attempt < 8; ++attempt) {
        temporary_directory = path.parent_path() /
            (".local-agent-normalize-output-" + std::to_string(serial.fetch_add(1)));
        std::error_code create_error;
        if (fs::create_directory(temporary_directory, create_error))
            break;
        temporary_directory.clear();
    }
    if (temporary_directory.empty())
        throw std::runtime_error("The normalization output could not be staged.");
    const fs::path temporary = temporary_directory / "result.json";
    {
        std::ofstream out(temporary, std::ios::binary | std::ios::out | std::ios::trunc);
        if (!out) {
            std::error_code cleanup;
            fs::remove(temporary_directory, cleanup);
            throw std::runtime_error("The normalization output could not be written.");
        }
        out << result.dump(2) << '\n';
        out.flush();
        if (!out) {
            std::error_code cleanup;
            fs::remove(temporary, cleanup);
            fs::remove(temporary_directory, cleanup);
            throw std::runtime_error("The normalization output could not be written.");
        }
    }
    std::error_code ec;
    fs::create_hard_link(temporary, path, ec);
    std::error_code cleanup;
    fs::remove(temporary, cleanup);
    fs::remove(temporary_directory, cleanup);
    if (ec)
        throw std::runtime_error("The normalization output could not be finalized.");
}

const std::set<std::string> &normalization_metadata_keys()
{
    static const std::set<std::string> keys = {
        "name", "type", "inherits", "from", "setting_id", "instantiation", "version",
        "filament_id", "description", "compatible_printers", "compatible_printers_condition",
        "printer_settings_id", "print_settings_id", "filament_settings_id"
    };
    return keys;
}

json normalize_profile(const fs::path &path, const fs::path &scratch_parent)
{
    const json raw = read_strict_json_file(path, 8 * 1024 * 1024);
    if (!raw.is_object())
        throw std::runtime_error("A profile must be a JSON object.");

    // First submit the actual file to Creality Print's config loader with an
    // empty config and disabled substitutions. It may report an unknown key;
    // the filtered second pass below exists only so every original key can be
    // classified without defaults or silently accepting unknown values.
    DynamicPrintConfig full_probe;
    ConfigSubstitutionContext probe_substitutions(ForwardCompatibilitySubstitutionRule::Disable);
    std::map<std::string, std::string> probe_metadata;
    std::string probe_reason;
    try {
        (void)full_probe.load_from_json(path.string(), probe_substitutions, false, probe_metadata, probe_reason);
    } catch (...) {
        // The accounting pass below reports unsupported keys without defaults.
    }

    // The native loader throws on unknown scalar keys, which would prevent
    // accounting later keys. Parse only schema-current keys plus the one
    // reviewed wall-order migration, while the accounting below still covers
    // every key from the original profile.
    json native_input = json::object();
    DynamicPrintConfig schema;
    const ConfigDef *definition = schema.def();
    for (const auto &item : raw.items()) {
        const std::string &key = item.key();
        if (key == "bed_type" || key == "adaptive_layer_height" || normalization_metadata_keys().count(key))
            continue;
        if (key == "wall_infill_order") {
            if (item.value().is_string()) {
                const std::string &value = item.value().get_ref<const std::string &>();
                const std::set<std::string> reviewed_values = {
                    "inner wall/outer wall/infill", "infill/inner wall/outer wall",
                    "outer wall/inner wall/infill", "infill/outer wall/inner wall",
                    "inner-outer-inner wall/infill"
                };
                if (reviewed_values.count(value))
                    native_input[key] = item.value();
            }
            continue;
        }
        const ConfigOptionDef *option_definition = definition == nullptr ? nullptr : definition->get(key);
        if (option_definition != nullptr && numeric_config_option_type(option_definition->type) &&
            contains_nonfinite_config_token(item.value()))
            throw std::runtime_error("A profile contains a non-finite numeric value.");
        if (option_definition != nullptr)
            native_input[key] = item.value();
    }

    static std::atomic<unsigned long long> temp_serial{0};
    fs::path temp_directory;
    for (int attempt = 0; attempt < 8; ++attempt) {
        temp_directory = scratch_parent /
            (".local-agent-normalize-" + std::to_string(temp_serial.fetch_add(1)));
        std::error_code ec;
        if (fs::create_directory(temp_directory, ec))
            break;
        temp_directory.clear();
    }
    if (temp_directory.empty())
        throw std::runtime_error("A private normalization workspace could not be created.");
    const fs::path native_path = temp_directory / "profile.json";
    {
        std::ofstream out(native_path, std::ios::binary | std::ios::out | std::ios::trunc);
        if (!out) {
            std::error_code cleanup;
            fs::remove(temp_directory, cleanup);
            throw std::runtime_error("A private normalization workspace could not be written.");
        }
        out << native_input.dump();
        out.flush();
        if (!out) {
            std::error_code cleanup;
            fs::remove(native_path, cleanup);
            fs::remove(temp_directory, cleanup);
            throw std::runtime_error("A private normalization workspace could not be written.");
        }
    }
    DynamicPrintConfig explicit_config;
    ConfigSubstitutionContext substitutions(ForwardCompatibilitySubstitutionRule::Disable);
    std::map<std::string, std::string> metadata;
    std::string reason;
    const int parse_status = explicit_config.load_from_json(native_path.string(), substitutions, false, metadata, reason);
    std::error_code cleanup;
    fs::remove(native_path, cleanup);
    fs::remove(temp_directory, cleanup);
    if (parse_status != 0 || !reason.empty() || !substitutions.empty())
        throw std::runtime_error("A profile contains values unsupported by the native configuration schema.");

    if (raw.contains("wall_infill_order") && raw["wall_infill_order"].is_string()) {
        const std::string wall_order = raw["wall_infill_order"].get<std::string>();
        const bool infill_first = wall_order == "infill/inner wall/outer wall" ||
                                  wall_order == "infill/outer wall/inner wall";
        if (raw.contains("is_infill_first")) {
            bool explicit_infill_first;
            const json &raw_value = raw["is_infill_first"];
            if (raw_value.is_boolean()) {
                explicit_infill_first = raw_value.get<bool>();
            } else if (raw_value.is_string() && (raw_value.get<std::string>() == "1" ||
                                                  raw_value.get<std::string>() == "true")) {
                explicit_infill_first = true;
            } else if (raw_value.is_string() && (raw_value.get<std::string>() == "0" ||
                                                  raw_value.get<std::string>() == "false")) {
                explicit_infill_first = false;
            } else {
                throw std::runtime_error("A wall-order profile contains an ambiguous infill-order setting.");
            }
            if (explicit_infill_first != infill_first)
                throw std::runtime_error("A profile contains conflicting wall-order settings.");
        }
        ConfigSubstitutionContext strict(ForwardCompatibilitySubstitutionRule::Disable);
        explicit_config.set_deserialize("is_infill_first", infill_first ? "1" : "0", strict);
    }

    json values = json::object();
    for (const std::string &key : explicit_config.keys()) {
        const ConfigOption *option = explicit_config.option(key);
        if (option == nullptr)
            continue;
        reject_nonfinite_config_option(*option);
        json value{{"type", static_cast<int>(option->type())}, {"serialized", option->serialize()}};
        if (const auto *vector_option = dynamic_cast<const ConfigOptionVectorBase *>(option))
            value["vector_size"] = vector_option->size();
        values[key] = std::move(value);
    }

    bool safe = true;
    json accounting = json::array();
    for (const auto &item : raw.items()) {
        const std::string &key = item.key();
        json canonical = json::array();
        std::string classification;
        if (normalization_metadata_keys().count(key)) {
            classification = "metadata";
        } else if (key == "adaptive_layer_height") {
            const bool explicitly_disabled =
                (item.value().is_string() && item.value().get<std::string>() == "0") ||
                (item.value().is_number_integer() && item.value().get<int64_t>() == 0) ||
                (item.value().is_boolean() && !item.value().get<bool>());
            if (explicitly_disabled) {
                classification = "obsolete_disabled";
            } else {
                classification = "hold_obsolete_or_unsupported";
                safe = false;
            }
        } else if (key == "wall_infill_order" && item.value().is_string()) {
            const std::string raw_value = item.value().get<std::string>();
            const std::set<std::string> reviewed_values = {
                "inner wall/outer wall/infill", "infill/inner wall/outer wall",
                "outer wall/inner wall/infill", "infill/outer wall/inner wall",
                "inner-outer-inner wall/infill"
            };
                if (reviewed_values.count(raw_value) && explicit_config.has("wall_sequence") &&
                    explicit_config.has("is_infill_first")) {
                    canonical.push_back("wall_sequence");
                    canonical.push_back("is_infill_first");
                classification = "mapped_current";
            } else {
                classification = "hold_unsupported_legacy_mapping";
                safe = false;
            }
        } else if (explicit_config.has(key)) {
            canonical.push_back(key);
            classification = "current";
        } else {
            classification = "hold_unknown_or_dropped";
            safe = false;
        }
        accounting.push_back({{"raw_key", key}, {"canonical_keys", canonical}, {"classification", classification}});
    }

    return json{{"sha256", sha256_file(path)}, {"values", values}, {"accounting", accounting}, {"safe", safe}};
}

fs::path checked_file(const json &value)
{
    if (!value.is_string())
        throw std::runtime_error("The request contains an invalid local file reference.");
    const fs::path path = fs::u8path(value.get<std::string>());
    if (!path.is_absolute())
        throw std::runtime_error("Local file references must be absolute paths.");
    std::error_code ec;
    const fs::path canonical = fs::canonical(path, ec);
    if (ec || !fs::is_regular_file(canonical, ec) || ec)
        throw std::runtime_error("A required local file is missing or not a regular file.");
    return canonical;
}

void write_result(const fs::path &result_path, const json &result)
{
    if (fs::exists(result_path))
        throw std::runtime_error("The helper result already exists.");
    const fs::path temporary = result_path.parent_path() /
        (".result.json.tmp-" + std::to_string(wxGetProcessId()));
    {
        std::ofstream out(temporary, std::ios::binary | std::ios::out | std::ios::trunc);
        if (!out)
            throw std::runtime_error("The helper result could not be written.");
        out << result.dump(2) << '\n';
        out.flush();
        if (!out)
            throw std::runtime_error("The helper result could not be written.");
    }
    std::error_code ec;
    fs::create_hard_link(temporary, result_path, ec);
    if (ec) {
        fs::remove(temporary);
        throw std::runtime_error("The helper result could not be finalized.");
    }
    fs::remove(temporary, ec);
}

DynamicPrintConfig load_flat_profile(PresetCollection &collection, const fs::path &path, const std::string &expected_type)
{
    if (fs::file_size(path) > 4 * 1024 * 1024)
        throw std::runtime_error("A local profile exceeds the supported size.");
    std::ifstream in(path, std::ios::binary);
    json raw;
    try {
        in >> raw;
    } catch (...) {
        throw std::runtime_error("A local profile is not valid JSON.");
    }
    if (!raw.is_object() || raw.value("type", std::string()) != expected_type ||
        !raw.contains("name") || !raw["name"].is_string() ||
        (raw.contains("inherits") && !raw["inherits"].is_null() && raw["inherits"] != ""))
        throw std::runtime_error("Only flattened local machine, process, and filament profiles are supported.");

    DynamicPrintConfig config;
    std::map<std::string, std::string> metadata;
    std::string reason;
    try {
        const ConfigSubstitutions substitutions = config.load_from_json(
            path.string(), ForwardCompatibilitySubstitutionRule::Disable, metadata, reason);
        if (!substitutions.empty() || !reason.empty())
            throw std::runtime_error("profile substitutions are unsupported");
    } catch (...) {
        throw std::runtime_error("A local profile contains unsupported settings.");
    }
    const std::string name = raw["name"].get<std::string>();
    if (name.empty() || name.size() > 200 || name.find_first_of("/\\\r\n") != std::string::npos)
        throw std::runtime_error("A local profile has an invalid name.");

    // `load_preset(DynamicPrintConfig&&)` installs the config verbatim. The
    // external JSON contains only explicit preset values, so first layer those
    // over the type/technology defaults; normal GUI/sidebar code expects the
    // full preset schema to be present even for omitted optional settings.
    DynamicPrintConfig complete_config(collection.default_preset_for(config).config);
    complete_config.apply(config);
    collection.load_preset(path.string(), name, std::move(complete_config), true);
    return config;
}

json read_manifest(const fs::path &path)
{
    std::error_code ec;
    if (!fs::is_regular_file(path, ec) || ec || fs::file_size(path, ec) > 1024 * 1024 || ec)
        throw std::runtime_error("The helper request is missing or too large.");
    std::ifstream in(path, std::ios::binary);
    json request;
    try {
        in >> request;
    } catch (...) {
        throw std::runtime_error("The helper request is not valid JSON.");
    }
    if (!request.is_object() || request.value("version", 0) != 1 ||
        !request.contains("request_id") || !request["request_id"].is_string() ||
        !request.contains("job_id") || !request["job_id"].is_string())
        throw std::runtime_error("The helper request has an unsupported format.");
    return request;
}

json run_prepare(GUI_App &app, const fs::path &request_path)
{
    json request = read_manifest(request_path);
    const fs::path model = checked_file(request.at("model_path"));
    const std::string expected_hash = request.value("input_sha256", std::string());
    const std::string actual_hash = sha256_file(model);
    if (expected_hash.size() != 64 || expected_hash != actual_hash)
        throw std::runtime_error("The input model changed after the request was created.");

    const std::string extension = model.extension().string();
    if (extension != ".stl" && extension != ".STL" && extension != ".obj" && extension != ".OBJ")
        throw std::runtime_error("Native preparation currently supports STL and OBJ inputs only; 3MF source preparation is not qualified.");
    if (!request.contains("settings") || !request["settings"].is_array() || request["settings"].size() != 2 ||
        !request.contains("filaments") || !request["filaments"].is_array() || request["filaments"].size() != 1)
        throw std::runtime_error("Preparation requires one machine, one process, and one filament profile.");
    if (request.value("copies", 0) != 1 || !request.contains("overrides") ||
        !request["overrides"].is_object() || !request["overrides"].empty())
        throw std::runtime_error("Copies and print-setting overrides are not yet supported by the native helper.");

    const fs::path requested_output = fs::u8path(request.at("output_project").get<std::string>()).lexically_normal();
    if (!requested_output.is_absolute())
        throw std::runtime_error("The output project path must be absolute.");
    std::error_code path_error;
    const fs::path output_parent = fs::canonical(requested_output.parent_path(), path_error);
    if (path_error || !fs::is_directory(output_parent) || requested_output.filename().empty())
        throw std::runtime_error("The output must be inside an existing job directory.");
    const fs::path output = output_parent / requested_output.filename();
    if (output.extension() != ".3mf" || fs::exists(output))
        throw std::runtime_error("The output must be a new .3mf file inside an existing job directory.");
    const fs::path result_path = output.parent_path() / "result.json";
    if (fs::exists(result_path))
        throw std::runtime_error("The helper result already exists.");

    const fs::path machine = checked_file(request["settings"][0]);
    const fs::path process = checked_file(request["settings"][1]);
    const fs::path filament = checked_file(request["filaments"][0]);
    const std::string settings_before = sha256_file(machine) + sha256_file(process) + sha256_file(filament);
    const DynamicPrintConfig explicit_machine = load_flat_profile(app.preset_bundle->printers, machine, "machine");
    const DynamicPrintConfig explicit_process = load_flat_profile(app.preset_bundle->prints, process, "process");
    const DynamicPrintConfig explicit_filament = load_flat_profile(app.preset_bundle->filaments, filament, "filament");
    if (settings_before != sha256_file(machine) + sha256_file(process) + sha256_file(filament))
        throw std::runtime_error("A local profile changed during preparation.");
    const std::string settings_hash = sha256_text(settings_before);
    app.load_current_presets(false, false);
    // Preset loading may normalize or replace some profile values. Reseed only
    // explicitly supplied keys that the native project schema can represent;
    // never copy schema defaults into the project configuration.
    auto seed_explicit_project_keys = [&app](const DynamicPrintConfig &explicit_config) {
        const t_config_option_keys project_keys = app.preset_bundle->project_config.keys();
        t_config_option_keys shared_keys;
        for (const std::string &key : project_keys)
            if (explicit_config.has(key))
                shared_keys.push_back(key);
        app.preset_bundle->project_config.apply_only(explicit_config, shared_keys, true);
    };
    seed_explicit_project_keys(explicit_machine);
    seed_explicit_project_keys(explicit_process);
    seed_explicit_project_keys(explicit_filament);

    const auto loaded = app.plater()->load_files(
        std::vector<boost::filesystem::path>{boost::filesystem::path(model.string())},
        LoadStrategy::LoadModel | LoadStrategy::Silence, false);
    if (loaded.empty() || app.plater()->model().objects.empty())
        throw std::runtime_error("The model could not be imported into the private Plater.");
    app.plater()->set_project_filename(wxString::FromUTF8(output.stem().string().c_str()));
    const fs::path temporary = output.parent_path() /
        (".local-agent-project-" + std::to_string(wxGetProcessId()) + ".3mf");
    if (fs::exists(temporary))
        throw std::runtime_error("A temporary project path is already in use.");
    int exported = -1;
    try {
        exported = app.plater()->export_3mf(boost::filesystem::path(temporary.string()), SaveStrategy::Silence, -1, nullptr, En3mfType::From_Creality);
    } catch (...) {
        std::error_code cleanup_error;
        fs::remove(temporary, cleanup_error);
        throw std::runtime_error("Creality Print could not export the editable project.");
    }
    if (exported != 0 || !fs::is_regular_file(temporary) || fs::file_size(temporary) == 0) {
        std::error_code cleanup_error;
        fs::remove(temporary, cleanup_error);
        throw std::runtime_error("Creality Print did not produce an editable project.");
    }
    if (sha256_file(model) != actual_hash) {
        std::error_code cleanup_error;
        fs::remove(temporary, cleanup_error);
        throw std::runtime_error("The source model changed during preparation.");
    }
    std::error_code publish_error;
    fs::create_hard_link(temporary, output, publish_error);
    if (publish_error) {
        fs::remove(temporary, publish_error);
        throw std::runtime_error("The editable project could not be finalized.");
    }
    fs::remove(temporary, publish_error);

    return json{{"version", 1}, {"request_id", request.at("request_id")}, {"ok", true},
                {"project_path", output.string()}, {"input_sha256", actual_hash},
                {"settings_sha256", settings_hash},
                {"warnings", json::array({"Geometry repair, orientation, and bed-fit qualification are not performed by this helper."})}};
}
} // namespace

int run_local_agent_normalize_cli(int argc, char **argv)
{
    if (argc != 3 || argv == nullptr || argv[1] == nullptr || argv[2] == nullptr ||
        std::string(argv[1]) != "--local-agent-normalize")
        return 2;

    fs::path output_path;
    try {
        const fs::path request_path = fs::u8path(argv[2]);
        if (!request_path.is_absolute())
            return 2;
        const json request = read_strict_json_file(fs::canonical(request_path), 1024 * 1024);
        if (!request.is_object() || request.value("version", 0) != 1 ||
            request.size() != 3 ||
            !request.contains("files") || !request["files"].is_array() ||
            request["files"].empty() || request["files"].size() > 4 ||
            !request.contains("output") || !request["output"].is_string())
            return 2;
        output_path = fs::u8path(request["output"].get<std::string>()).lexically_normal();
        if (!output_path.is_absolute())
            return 2;
        std::error_code ec;
        const fs::path parent = fs::canonical(output_path.parent_path(), ec);
        if (ec || !fs::is_directory(parent, ec) || ec || output_path.filename().empty() || fs::exists(output_path))
            return 2;
        output_path = parent / output_path.filename();

        json files = json::array();
        bool safe = true;
        for (const json &input : request["files"]) {
            if (!input.is_string())
                throw std::runtime_error("A profile path is invalid.");
            fs::path profile = fs::u8path(input.get<std::string>());
            if (!profile.is_absolute())
                throw std::runtime_error("Profile paths must be absolute.");
            profile = fs::canonical(profile, ec);
            if (ec || !fs::is_regular_file(profile, ec) || ec)
                throw std::runtime_error("A profile is unavailable.");
            const std::string before = sha256_file(profile);
            json normalized = normalize_profile(profile, parent);
            if (before != sha256_file(profile))
                throw std::runtime_error("A profile changed during normalization.");
            safe = safe && normalized.at("safe").get<bool>();
            files.push_back(std::move(normalized));
        }
        json result{{"version", 1}, {"ok", safe}, {"files", files}};
        if (!safe)
            result["error"] = "One or more profile keys are unsupported and require review.";
        write_atomic_new(output_path, result);
        return safe ? 0 : 1;
    } catch (const std::exception &) {
        if (!output_path.empty()) {
            try {
                write_atomic_new(output_path, json{{"version", 1}, {"ok", false},
                    {"error", "The native configuration normalizer could not process the request."}});
            } catch (...) {
            }
        }
        return 1;
    }
}

void run_local_agent_project_helper(GUI_App &app)
{
    const fs::path operand = fs::u8path(app.init_params->local_agent_argument);
    int exit_code = 0;
    bool keep_inspection_window_open = false;
    try {
        if (app.init_params->local_agent_action == "prepare") {
            json result;
            fs::path result_path;
            try {
                const json request = read_manifest(operand);
                const fs::path output = fs::u8path(request.at("output_project").get<std::string>()).lexically_normal();
                if (!output.is_absolute())
                    throw std::runtime_error("The output project path must be absolute.");
                std::error_code path_error;
                const fs::path output_parent = fs::canonical(output.parent_path(), path_error);
                if (path_error || !fs::is_directory(output_parent))
                    throw std::runtime_error("The output job directory is unavailable.");
                result_path = output_parent / "result.json";
            } catch (...) {
                throw std::runtime_error("The helper request could not be validated.");
            }
            try {
                result = run_prepare(app, operand);
                try {
                    write_result(result_path, result);
                } catch (...) {
                    std::error_code cleanup_error;
                    fs::remove(fs::u8path(result.at("project_path").get<std::string>()), cleanup_error);
                    throw;
                }
            } catch (const std::exception &) {
                const json request = read_manifest(operand);
                result = json{{"version", 1}, {"request_id", request.value("request_id", "")},
                              {"ok", false}, {"error", "The local project helper could not complete the request."}};
                write_result(result_path, result);
                exit_code = 1;
            }
        } else if (app.init_params->local_agent_action == "inspect") {
            if (operand.extension() != ".3mf" || !fs::is_regular_file(operand))
                throw std::runtime_error("Inspection requires an existing native 3MF project.");
            const auto loaded = app.plater()->load_files(
                std::vector<boost::filesystem::path>{boost::filesystem::path(fs::canonical(operand).string())},
                LoadStrategy::LoadModel | LoadStrategy::LoadConfig | LoadStrategy::LoadAuxiliary | LoadStrategy::Silence, false);
            if (loaded.empty())
                throw std::runtime_error("The project could not be opened in the private Plater.");
            app.mainframe->Show(true);
            keep_inspection_window_open = true;
        } else {
            throw std::runtime_error("Unsupported local-agent helper operation.");
        }
    } catch (const std::exception &) {
        BOOST_LOG_TRIVIAL(error) << "Local-agent helper failed with a sanitized error.";
        exit_code = 1;
    }
    if (keep_inspection_window_open)
        return;
    app.set_local_agent_helper_exit_code(exit_code);
    wxTheApp->ExitMainLoop();
}
} }
