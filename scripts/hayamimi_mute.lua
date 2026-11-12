-- =============================================================================
-- Hayamimi Live - OBS Microphone Mute Sync Script
-- 早耳 (Hayamimi Live) と OBS Studio のマイクミュート状態を自動連動させるスクリプト
-- =============================================================================
obs = obslua

local server_url = "https://hayamimi.kit-a.net"
local obs_token = ""
local target_source_name = ""
local source_signal_handler = nil
local current_source_name = nil

function send_mute_status(muted)
    if obs_token == nil or obs_token == "" then
        return
    end

    local base_url = server_url:gsub("/+$", "")
    local endpoint = base_url .. "/api/user/mute?token=" .. obs_token
    local json_body = string.format('{\\"muted\\": %s}', tostring(muted))

    local cmd
    if package.config:sub(1,1) == '\\' then
        -- Windows: start /b でバックグラウンド非同期実行 (黒いプロンプト画面を表示させない)
        cmd = string.format('start /b curl.exe -s -X POST -H "Content-Type: application/json" -d "%s" "%s"', json_body, endpoint)
    else
        -- Linux / macOS
        cmd = string.format('curl -s -X POST -H "Content-Type: application/json" -d "%s" "%s" &', json_body, endpoint)
    end

    os.execute(cmd)
    obs.blog(obs.LOG_INFO, string.format("[Hayamimi] Mute status synced: %s (source: %s)", tostring(muted), tostring(current_source_name)))
end

function on_source_mute(calldata)
    local muted = obs.calldata_bool(calldata, "muted")
    send_mute_status(muted)
end

function hook_source(source_name)
    if source_signal_handler ~= nil then
        obs.signal_handler_disconnect(source_signal_handler, "mute", on_source_mute)
        source_signal_handler = nil
    end

    local source = nil
    if source_name and source_name ~= "" then
        source = obs.obs_get_source_by_name(source_name)
    else
        -- 自動検出: 最初の音声入力キャプチャソースを検出
        local sources = obs.obs_enum_sources()
        if sources ~= nil then
            for _, s in ipairs(sources) do
                local id = obs.obs_source_get_id(s)
                if id == "wasapi_input_capture" or id == "coreaudio_input_capture" or id == "pulse_input_capture" or id == "alsa_input_capture" then
                    source = s
                    break
                end
            end
            obs.source_list_release(sources)
        end
    end

    if source ~= nil then
        current_source_name = obs.obs_source_get_name(source)
        source_signal_handler = obs.obs_source_get_signal_handler(source)
        obs.signal_handler_connect(source_signal_handler, "mute", on_source_mute)
        
        -- 初期接続時のミュート状態を即時送信
        local is_muted = obs.obs_source_muted(source)
        send_mute_status(is_muted)
        
        obs.obs_source_release(source)
        obs.blog(obs.LOG_INFO, "[Hayamimi] Hooked to audio source: " .. current_source_name)
    else
        obs.blog(obs.LOG_WARNING, "[Hayamimi] Audio source not found: " .. tostring(source_name))
    end
end

function script_update(settings)
    server_url = obs.obs_data_get_string(settings, "server_url")
    obs_token = obs.obs_data_get_string(settings, "obs_token")
    target_source_name = obs.obs_data_get_string(settings, "source_name")
    hook_source(target_source_name)
end

function script_properties()
    local props = obs.obs_properties_create()
    obs.obs_properties_add_text(props, "server_url", "早耳サーバーURL", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "obs_token", "OBS連携トークン", obs.OBS_TEXT_DEFAULT)

    local p_sources = obs.obs_properties_add_list(props, "source_name", "監視マイクソース", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(p_sources, "自動検出 (Mic/Aux または最初のマイク)", "")
    
    local sources = obs.obs_enum_sources()
    if sources ~= nil then
        for _, s in ipairs(sources) do
            local id = obs.obs_source_get_id(s)
            if id == "wasapi_input_capture" or id == "coreaudio_input_capture" or id == "pulse_input_capture" or id == "alsa_input_capture" then
                local name = obs.obs_source_get_name(s)
                obs.obs_property_list_add_string(p_sources, name, name)
            end
        end
        obs.source_list_release(sources)
    end
    return props
end

function script_defaults(settings)
    obs.obs_data_set_default_string(settings, "server_url", "https://hayamimi.kit-a.net")
    obs.obs_data_set_default_string(settings, "obs_token", "")
    obs.obs_data_set_default_string(settings, "source_name", "")
end

function script_load(settings)
    script_update(settings)
end

function script_unload()
    if source_signal_handler ~= nil then
        obs.signal_handler_disconnect(source_signal_handler, "mute", on_source_mute)
        source_signal_handler = nil
    end
end

function script_description()
    return "早耳 (Hayamimi Live) と OBS のマイクミュート状態を自動連動させます。\nOBS 側でマイクがミュートされた時、早耳の文字起こしを自動休止します。\n\n※Standalone版で使用する場合は、サーバーURLを http://127.0.0.1:8833 に設定してください。"
end

