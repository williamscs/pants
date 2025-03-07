use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;
use bazel_protos::gen::build::bazel::remote::execution::v2 as remexec;
use bytes::Bytes;
use futures::{future, FutureExt};
use hashing::Fingerprint;
use log::{debug, warn};
use prost::Message;
use serde::{Deserialize, Serialize};
use sharded_lmdb::ShardedLmdb;
use store::Store;
use workunit_store::{
  in_workunit, Level, Metric, ObservationMetric, RunningWorkunit, WorkunitMetadata,
};

use crate::{
  Context, FallibleProcessResultWithPlatform, MultiPlatformProcess, Platform, Process,
  ProcessCacheScope, ProcessMetadata, ProcessResultSource,
};

#[allow(dead_code)]
#[derive(Serialize, Deserialize)]
struct PlatformAndResponseBytes {
  platform: Platform,
  response_bytes: Vec<u8>,
}

#[derive(Clone)]
pub struct CommandRunner {
  underlying: Arc<dyn crate::CommandRunner>,
  process_execution_store: ShardedLmdb,
  file_store: Store,
  metadata: ProcessMetadata,
}

impl CommandRunner {
  pub fn new(
    underlying: Arc<dyn crate::CommandRunner>,
    process_execution_store: ShardedLmdb,
    file_store: Store,
    metadata: ProcessMetadata,
  ) -> CommandRunner {
    CommandRunner {
      underlying,
      process_execution_store,
      file_store,
      metadata,
    }
  }
}

#[async_trait]
impl crate::CommandRunner for CommandRunner {
  fn extract_compatible_request(&self, req: &MultiPlatformProcess) -> Option<Process> {
    self.underlying.extract_compatible_request(req)
  }

  async fn run(
    &self,
    context: Context,
    workunit: &mut RunningWorkunit,
    req: MultiPlatformProcess,
  ) -> Result<FallibleProcessResultWithPlatform, String> {
    let cache_lookup_start = Instant::now();
    let write_failures_to_cache = req
      .0
      .values()
      .any(|process| process.cache_scope == ProcessCacheScope::Always);
    let digest = crate::digest(req.clone(), &self.metadata);
    let key = digest.hash;

    let context2 = context.clone();
    let cache_read_result = in_workunit!(
      context.workunit_store.clone(),
      "local_cache_read".to_owned(),
      WorkunitMetadata {
        level: Level::Trace,
        desc: Some(format!("Local cache lookup: {}", req.user_facing_name())),
        ..WorkunitMetadata::default()
      },
      |workunit| async move {
        workunit.increment_counter(Metric::LocalCacheRequests, 1);

        match self.lookup(key).await {
          Ok(Some(result)) if result.exit_code == 0 || write_failures_to_cache => {
            let lookup_elapsed = cache_lookup_start.elapsed();
            workunit.increment_counter(Metric::LocalCacheRequestsCached, 1);
            if let Some(time_saved) = result.metadata.time_saved_from_cache(lookup_elapsed) {
              let time_saved = time_saved.as_millis() as u64;
              workunit.increment_counter(Metric::LocalCacheTotalTimeSavedMs, time_saved);
              context2
                .workunit_store
                .record_observation(ObservationMetric::LocalCacheTimeSavedMs, time_saved);
            }
            // When we successfully use the cache, we change the description and increase the level
            // (but not so much that it will be logged by default).
            workunit.update_metadata(|initial| WorkunitMetadata {
              desc: initial.desc.as_ref().map(|desc| format!("Hit: {}", desc)),
              level: Level::Debug,
              ..initial
            });
            Ok(result)
          }
          Err(err) => {
            debug!(
              "Error loading process execution result from local cache: {} - continuing to execute",
              err
            );
            workunit.increment_counter(Metric::LocalCacheReadErrors, 1);
            // Falling through to re-execute.
            Err(())
          }
          Ok(_) => {
            // Either we missed, or we hit for a failing result.
            workunit.increment_counter(Metric::LocalCacheRequestsUncached, 1);
            // Falling through to execute.
            Err(())
          }
        }
      }
      .boxed()
    )
    .await;

    if let Ok(result) = cache_read_result {
      return Ok(result);
    }

    let result = self.underlying.run(context.clone(), workunit, req).await?;
    if result.exit_code == 0 || write_failures_to_cache {
      let result = result.clone();
      in_workunit!(
        context.workunit_store.clone(),
        "local_cache_write".to_owned(),
        WorkunitMetadata {
          level: Level::Trace,
          ..WorkunitMetadata::default()
        },
        |workunit| async move {
          if let Err(err) = self.store(key, &result).await {
            warn!(
              "Error storing process execution result to local cache: {} - ignoring and continuing",
              err
            );
            workunit.increment_counter(Metric::LocalCacheWriteErrors, 1);
          }
        }
      )
      .await;
    }
    Ok(result)
  }
}

impl CommandRunner {
  async fn lookup(
    &self,
    fingerprint: Fingerprint,
  ) -> Result<Option<FallibleProcessResultWithPlatform>, String> {
    use remexec::ExecuteResponse;

    // See whether there is a cache entry.
    let maybe_execute_response: Option<(ExecuteResponse, Platform)> = self
      .process_execution_store
      .load_bytes_with(fingerprint, move |bytes| {
        let decoded: PlatformAndResponseBytes = bincode::deserialize(bytes)
          .map_err(|err| format!("Could not deserialize platform and response: {}", err))?;
        let platform = decoded.platform;
        let execute_response = ExecuteResponse::decode(&decoded.response_bytes[..])
          .map_err(|e| format!("Invalid ExecuteResponse: {:?}", e))?;
        Ok((execute_response, platform))
      })
      .await?;

    // Deserialize the cache entry if it existed.
    let result = if let Some((execute_response, platform)) = maybe_execute_response {
      if let Some(ref action_result) = execute_response.result {
        crate::remote::populate_fallible_execution_result(
          self.file_store.clone(),
          action_result,
          platform,
          true,
          ProcessResultSource::HitLocally,
        )
        .await?
      } else {
        return Err("action result missing from ExecuteResponse".into());
      }
    } else {
      return Ok(None);
    };

    // Ensure that all digests in the result are loadable, erroring if any are not.
    let _ = future::try_join_all(vec![
      self
        .file_store
        .ensure_local_has_file(result.stdout_digest)
        .boxed(),
      self
        .file_store
        .ensure_local_has_file(result.stderr_digest)
        .boxed(),
      self
        .file_store
        .ensure_local_has_recursive_directory(result.output_directory),
    ])
    .await?;

    Ok(Some(result))
  }

  async fn store(
    &self,
    fingerprint: Fingerprint,
    result: &FallibleProcessResultWithPlatform,
  ) -> Result<(), String> {
    let stdout_digest = result.stdout_digest;
    let stderr_digest = result.stderr_digest;

    let action_result = remexec::ActionResult {
      exit_code: result.exit_code,
      output_directories: vec![remexec::OutputDirectory {
        path: String::new(),
        tree_digest: Some((&result.output_directory).into()),
      }],
      stdout_digest: Some((&stdout_digest).into()),
      stderr_digest: Some((&stderr_digest).into()),
      execution_metadata: Some(result.metadata.clone().into()),
      ..remexec::ActionResult::default()
    };
    let execute_response = remexec::ExecuteResponse {
      cached_result: true,
      result: Some(action_result),
      ..remexec::ExecuteResponse::default()
    };

    // TODO: Should probably have a configurable lease time which is larger than default.
    // (This isn't super urgent because we don't ever actually GC this store. So also...)
    // TODO: GC the local process execution cache.

    let mut response_bytes = Vec::with_capacity(execute_response.encoded_len());
    execute_response
      .encode(&mut response_bytes)
      .map_err(|err| format!("Error serializing execute process result to cache: {}", err))?;

    let bytes_to_store = bincode::serialize(&PlatformAndResponseBytes {
      platform: result.platform,
      response_bytes,
    })
    .map(Bytes::from)
    .map_err(|err| {
      format!(
        "Error serializing platform and execute process result: {}",
        err
      )
    })?;

    self
      .process_execution_store
      .store_bytes(fingerprint, bytes_to_store, false)
      .await
  }
}
