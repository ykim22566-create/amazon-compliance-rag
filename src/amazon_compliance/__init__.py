"""Amazon Seller Central compliance ingestion + freshness module.

Pipeline overview:
    SourceRegistry -> HelpFetcher -> SnapshotStore -> HtmlTableParser
                                  -> ChangeDetector -> IngestionPipeline
"""
