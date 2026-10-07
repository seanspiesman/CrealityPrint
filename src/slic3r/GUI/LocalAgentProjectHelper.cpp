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
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <map>
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

void load_flat_profile(PresetCollection &collection, const fs::path &path, const std::string &expected_type)
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
    collection.load_preset(path.string(), name, std::move(config), true);
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
    load_flat_profile(app.preset_bundle->printers, machine, "machine");
    load_flat_profile(app.preset_bundle->prints, process, "process");
    load_flat_profile(app.preset_bundle->filaments, filament, "filament");
    if (settings_before != sha256_file(machine) + sha256_file(process) + sha256_file(filament))
        throw std::runtime_error("A local profile changed during preparation.");
    const std::string settings_hash = sha256_text(settings_before);
    app.load_current_presets(false, false);

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
