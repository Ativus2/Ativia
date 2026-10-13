"""Replaceable speech recognition backends, imported only when requested."""
import re

def hotwords_from_settings(settings):
    if settings.hotwords_enabled != 'yes': return ()
    # Commas/newlines delimit terms. Spaces within a name are preserved.
    return tuple(dict.fromkeys(word.strip() for word in re.split(r'[,，;；\n]', settings.hotwords)
                              if word.strip()))[:100]

def make_provider(settings):
    options = dict(language=settings.asr_language, hotwords=hotwords_from_settings(settings))
    if settings.backend == 'openai':
        from .openai_asr import OpenAIASRProvider
        from .commit_controller import EndpointConfig
        return OpenAIASRProvider(**options, endpoint_ms=int(settings.endpoint_ms), prompt=settings.glossary,
                                 endpoint_config=EndpointConfig.from_settings(settings))
    if settings.backend == 'deepgram':
        from .deepgram_asr import DeepgramASR
        return DeepgramASR(**options, endpoint_ms=int(settings.endpoint_ms))
    from .windows_asr import WindowsASR
    return WindowsASR(**options, endpoint_ms=int(settings.endpoint_ms),
                      close_owned=settings.close_owned == 'yes')
