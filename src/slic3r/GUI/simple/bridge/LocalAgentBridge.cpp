#include "LocalAgentBridge.hpp"

#include "slic3r/GUI/GUI_App.hpp"

#include <curl/curl.h>
#include <wx/utils.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <set>
#include <thread>

namespace Slic3r {
namespace GUI {
namespace Bridge {
namespace {

const std::set<std::string> kActions = {
    "state", "create_job", "select_model", "prepare_job", "queue_job", "start_job",
    "pause_job", "cancel_job", "resume_job", "approve_resume", "approve_budget",
    "save_model", "save_policy", "enroll_printer", "save_profile", "enroll_reference",
    "save_cfs_inventory", "probe_printer", "open_project", "chat", "new_conversation",
    "get_conversation", "acknowledge_alert", "answer_question"
};

std::string read_owner_token()
{
    std::string home;
    if (const char* configured = std::getenv("CREALITY_AGENT_HOME"))
        home = configured;
    if (home.empty())
        home = (wxGetHomeDir() + "/Library/Application Support/CrealityAgent/runtime").ToStdString();

    std::ifstream file(home + "/owner.token", std::ios::binary);
    if (!file)
        return {};
    std::string token((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    while (!token.empty() && std::isspace(static_cast<unsigned char>(token.back())))
        token.pop_back();
    auto first = std::find_if_not(token.begin(), token.end(), [](unsigned char c) { return std::isspace(c); });
    token.erase(token.begin(), first);
    return token;
}

bool sensitive_key(const std::string& key)
{
    std::string lower = key;
    std::transform(lower.begin(), lower.end(), lower.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return lower == "token" || lower == "owner_token" || lower == "agent_token" ||
           lower == "api_key" || lower == "authorization" || lower == "password" ||
           lower == "secret" || lower == "image" || lower == "image_bytes" ||
           lower == "image_data" || lower == "preview_image" || lower == "thumbnail" ||
           lower == "frame" || lower == "frame_bytes" || lower == "frame_data" ||
           lower == "camera_frame" || lower == "pixels" || lower == "pixel_data" ||
           lower == "detector_payload";
}

LocalAgentBridge::Json sanitize(const LocalAgentBridge::Json& value, const std::string& token,
                                bool allow_project_path = false)
{
    if (value.is_object()) {
        auto result = LocalAgentBridge::Json::object();
        for (auto it = value.begin(); it != value.end(); ++it) {
            if (!sensitive_key(it.key()) && (allow_project_path || it.key() != "project_path"))
                result[it.key()] = sanitize(it.value(), token);
        }
        return result;
    }
    if (value.is_array()) {
        auto result = LocalAgentBridge::Json::array();
        for (const auto& entry : value)
            result.push_back(sanitize(entry, token));
        return result;
    }
    if (value.is_string() && !token.empty()) {
        std::string text = value.get<std::string>();
        std::size_t pos = 0;
        while ((pos = text.find(token, pos)) != std::string::npos) {
            text.replace(pos, token.size(), "[redacted]");
            pos += 10;
        }
        return text;
    }
    return value;
}

struct ResponseBuffer {
    std::string body;
    bool too_large = false;
};

size_t write_response(char* data, size_t size, size_t count, void* context)
{
    auto* response = static_cast<ResponseBuffer*>(context);
    const size_t bytes = size * count;
    if (response->body.size() + bytes > 2 * 1024 * 1024) {
        response->too_large = true;
        return 0;
    }
    response->body.append(data, bytes);
    return bytes;
}

struct CurlHeaders {
    curl_slist* value = nullptr;
    ~CurlHeaders() { curl_slist_free_all(value); }
    void add(const std::string& header) { value = curl_slist_append(value, header.c_str()); }
};

LocalAgentBridge::Json send_request(const std::string& token, const std::string& action,
                                    const LocalAgentBridge::Json& payload,
                                    const std::string& idempotency_key)
{
    CURL* curl = curl_easy_init();
    if (!curl)
        return {{"ok", false}, {"error", "Local service request could not be initialized"}};

    ResponseBuffer response;
    CurlHeaders headers;
    headers.add("Accept: application/json");
    headers.add(("Authorization: Bearer " + token));
    std::string request_body;
    const bool is_state = action == "state";
    std::string endpoint = "http://127.0.0.1:18088/v1/operator/state";
    if (!is_state) {
        endpoint = "http://127.0.0.1:18088/v1/operator/actions";
        headers.add("Content-Type: application/json");
        request_body = LocalAgentBridge::Json{
            {"action", action}, {"payload", payload}, {"idempotency_key", idempotency_key}
        }.dump();
    }

    curl_easy_setopt(curl, CURLOPT_URL, endpoint.c_str());
    curl_easy_setopt(curl, CURLOPT_PROTOCOLS, CURLPROTO_HTTP);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
    curl_easy_setopt(curl, CURLOPT_PROXY, "");
    curl_easy_setopt(curl, CURLOPT_NOPROXY, "*");
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 1500L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 15000L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers.value);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_response);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    if (!is_state) {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, request_body.data());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(request_body.size()));
    }

    const CURLcode status = curl_easy_perform(curl);
    long http_status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_status);
    curl_easy_cleanup(curl);
    if (status != CURLE_OK || response.too_large)
        return {{"ok", false}, {"error", "Local service is unavailable or returned an invalid response"}};

    LocalAgentBridge::Json result = LocalAgentBridge::Json::parse(response.body, nullptr, false);
    if (result.is_discarded())
        return {{"ok", false}, {"error", "Local service returned an invalid response"}};
    result = sanitize(result, token, action == "open_project");
    if (http_status < 200 || http_status >= 300) {
        std::string detail = "Local service rejected the request";
        if (result.is_object() && result.contains("detail") && result["detail"].is_string())
            detail = result["detail"].get<std::string>().substr(0, 600);
        return {{"ok", false}, {"error", detail}};
    }
    return {{"ok", true}, {"result", std::move(result)}};
}

} // namespace

LocalAgentBridge::LocalAgentBridge() : m_owner_token(read_owner_token()) {}

LocalAgentBridge::~LocalAgentBridge()
{
    m_lifetime.reset();
    m_owner_token.clear();
}

void LocalAgentBridge::Request(const std::string& request_id,
                               const std::string& action,
                               const Json& payload,
                               const std::string& idempotency_key,
                               Completion completion)
{
    auto finish = [request_id, completion](bool ok, const Json& result, const std::string& error) {
        completion({{"request_id", request_id}, {"ok", ok},
                    {"result", result}, {"error", error}});
    };
    if (!completion)
        return;
    if (request_id.empty() || request_id.size() > 128 || kActions.count(action) == 0 ||
        !payload.is_object() || payload.dump().size() > 1024 * 1024 ||
        (action != "state" && (idempotency_key.empty() || idempotency_key.size() > 128))) {
        finish(false, Json::object(), "Invalid local service request");
        return;
    }
    if (m_owner_token.empty()) {
        finish(false, Json::object(), "Owner token is unavailable; configure CREALITY_AGENT_HOME");
        return;
    }

    std::weak_ptr<int> lifetime = m_lifetime;
    std::string token = m_owner_token;
    std::thread([lifetime, token, request_id, action, payload, idempotency_key,
                 completion = std::move(completion)]() mutable {
        Json output = send_request(token, action, payload, idempotency_key);
        const bool ok = output.value("ok", false);
        const Json result = output.value("result", Json::object());
        const std::string error = output.value("error", std::string());
        if (!wxTheApp)
            return;
        wxTheApp->CallAfter([lifetime, completion = std::move(completion), request_id, ok,
                              result, error]() mutable {
            if (lifetime.expired())
                return;
            completion({{"request_id", request_id}, {"ok", ok},
                        {"result", result}, {"error", error}});
        });
    }).detach();
}

} // namespace Bridge
} // namespace GUI
} // namespace Slic3r
