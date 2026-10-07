#ifndef slic3r_GUI_simple_bridge_LocalAgentBridge_hpp_
#define slic3r_GUI_simple_bridge_LocalAgentBridge_hpp_

#include "nlohmann/json.hpp"

#include <functional>
#include <memory>
#include <string>

namespace Slic3r {
namespace GUI {
namespace Bridge {

class LocalAgentBridge
{
public:
    using Json = nlohmann::json;
    using Completion = std::function<void(const Json&)>;

    LocalAgentBridge();
    ~LocalAgentBridge();
    LocalAgentBridge(const LocalAgentBridge&) = delete;
    LocalAgentBridge& operator=(const LocalAgentBridge&) = delete;

    void Request(const std::string& request_id,
                 const std::string& action,
                 const Json& payload,
                 const std::string& idempotency_key,
                 Completion completion);

private:
    std::string m_owner_token;
    std::shared_ptr<int> m_lifetime = std::make_shared<int>(0);
};

} // namespace Bridge
} // namespace GUI
} // namespace Slic3r

#endif
