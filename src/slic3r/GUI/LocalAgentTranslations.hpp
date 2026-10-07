#pragma once

#include <wx/dir.h>
#include <wx/filename.h>
#include <wx/translation.h>

namespace Slic3r::GUI {

// Packaged apps must not scan the dependency build's install prefix. On macOS
// that prefix can be in Documents and block startup waiting for filesystem consent.
class LocalAgentTranslations final : public wxTranslationsLoader {
public:
    explicit LocalAgentTranslations(const wxString& root) : m_root(root) {}

    wxMsgCatalog* LoadCatalog(const wxString& domain, const wxString& language) override
    {
        if (m_root.empty() || !safe_component(domain) || !safe_component(language))
            return nullptr;
        for (const auto& suffix : {wxString(), wxString(".lproj")}) {
            const wxString directory = m_root + wxFILE_SEP_PATH + language + suffix;
            for (const auto& subdirectory : {wxString(), wxString("LC_MESSAGES")}) {
                const wxString file = wxFileName(directory + wxFILE_SEP_PATH + subdirectory,
                                                domain + ".mo").GetFullPath();
                if (wxFileExists(file))
                    return wxMsgCatalog::CreateFromFile(file, domain);
            }
        }
        return nullptr;
    }

    wxArrayString GetAvailableTranslations(const wxString& domain) const override
    {
        wxArrayString languages;
        if (m_root.empty() || !safe_component(domain))
            return languages;
        wxDir directory(m_root);
        if (!directory.IsOpened())
            return languages;
        wxString name;
        for (bool found = directory.GetFirst(&name, wxString(), wxDIR_DIRS);
             found; found = directory.GetNext(&name)) {
            const wxString path = m_root + wxFILE_SEP_PATH + name;
            if (wxFileExists(path + wxFILE_SEP_PATH + domain + ".mo") ||
                wxFileExists(path + "/LC_MESSAGES/" + domain + ".mo")) {
                wxString language = name;
                name.EndsWith(".lproj", &language);
                languages.Add(language);
            }
        }
        return languages;
    }

private:
    static bool safe_component(const wxString& value)
    {
        return !value.empty() && value != "." && value != ".." &&
               value.Find('/') == wxNOT_FOUND && value.Find('\\') == wxNOT_FOUND;
    }
    wxString m_root;
};

} // namespace Slic3r::GUI
